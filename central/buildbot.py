"""Buildbot module that handles communications between the Buildbot and
GitHub."""

from config import cfg

import events
import utils

import collections
import json
import logging
import os
import os.path
import queue
import re
import requests
import uuid


def make_netstring(s):
    """Creates a netstring from a blob of bytes."""
    return str(len(s)).encode('ascii') + b':' + s + b','


def make_build_request(repo, pr_id, job_id, baserev, headrev, who, comment):
    """Creates a build request binary blob in the format expected by the
    buildbot."""

    request_dict = {
        'branch': 'refs/pull/%d/head' % pr_id,
        'builderNames': cfg.buildbot.pr_builders,
        'jobid': job_id,
        'baserev': '',
        'patch_level': 0,
        'patch_body': None,
        'who': who,
        'comment': comment,
        'properties': {
            'branchname': 'pr-%d' % pr_id,
            'baserev': baserev,
            'headrev': headrev,
            'shortrev': headrev[:6],
            'pr_id': pr_id,
            'repo': repo,
        },
    }
    encoded = json.dumps(request_dict, ensure_ascii=True).encode('ascii')
    version = make_netstring(b'5')
    return version + make_netstring(encoded)


def send_build_request(build_request):
    """Stores the build request (atomically) in the buildbot jobdir."""

    path = os.path.join(cfg.buildbot.jobdir, 'tmp', str(uuid.uuid4()))
    open(path, 'wb').write(build_request)
    final_path = os.path.join(cfg.buildbot.jobdir, 'new', str(uuid.uuid4()))
    os.rename(path, final_path)
    logging.info('Sent build request: %r', final_path)


class PullRequestBuilder:
    def __init__(self):
        self.queue = queue.Queue()

    def push(self, in_behalf_of, trusted, repo, pr_id):
        self.queue.put((in_behalf_of, trusted, repo, pr_id))

    def run(self):
        while True:
            in_behalf_of, trusted, repo, pr_id = self.queue.get()

            # To check if a PR is mergeable, we need to request it directly.
            pr = requests.get('https://api.github.com/repos/%s/pulls/%d' %
                              (repo, pr_id)).json()
            logging.info('PR %s mergeable: %s (%s)', pr_id, pr['mergeable'],
                         pr['mergeable_state'])

            base_sha = pr['base']['sha']
            head_sha = pr['head']['sha']

            shortrev = head_sha[:6]

            if not trusted:
                status_evt = events.BuildStatus(
                    repo, head_sha, shortrev, 'default', pr_id, False, False,
                    '', 'PR not built because %s is not auto-trusted.' %
                    in_behalf_of)
                events.dispatcher.dispatch('prbuilder', status_evt)
                continue

            # mergeable can be None!
            if pr['mergeable'] is False:
                status_evt = events.BuildStatus(
                    repo, head_sha, shortrev, 'default', pr_id, False, False,
                    '', 'PR cannot be merged, please rebase.')
                events.dispatcher.dispatch('prbuilder', status_evt)
                continue

            status_evt = events.BuildStatus(
                repo, head_sha, shortrev, 'default', pr_id, True, False, '',
                'Very basic checks passed, handed off to Buildbot.')
            events.dispatcher.dispatch('prbuilder', status_evt)

            for builder in cfg.buildbot.pr_builders:
                status_evt = events.BuildStatus(repo, head_sha, shortrev, builder,
                                                pr_id, False, True, cfg.buildbot.url,
                                                'Auto build pending')
                events.dispatcher.dispatch('prbuilder', status_evt)

            req = make_build_request(
                repo, pr_id, '%d-%s' % (pr_id, head_sha[:6]), base_sha, head_sha,
                'Central (on behalf of: %s)' % in_behalf_of,
                'Auto build for PR #%d (%s).' % (pr_id, head_sha))
            send_build_request(req)


class PullRequestListener(events.EventTarget):
    """Listens for new or synchronized pull requests and starts a new build."""

    def __init__(self, builder):
        super(PullRequestListener, self).__init__()
        self.builder = builder

    def accept_event(self, evt):
        return evt.type == events.GHPullRequest.TYPE

    def push_event(self, evt):
        if evt.action == 'opened' or evt.action == 'synchronize':
            if evt.repo in cfg.github.maintain:
                self.builder.push(evt.author, evt.safe_author, evt.repo,
                                  evt.id)


class ManualPullRequestListener(events.EventTarget):
    """Listens for comments from trusted users on PRs for a keyword to build
    a PR from an untrusted user."""

    def __init__(self, builder):
        super(ManualPullRequestListener, self).__init__()
        self.builder = builder

    def accept_event(self, evt):
        return evt.type == events.GHIssueComment.TYPE

    def push_event(self, evt):
        if not evt.safe_author:
            return
        if cfg.github.rebuild_command.lower() not in evt.body.lower():
            return
        if evt.repo not in cfg.github.maintain:
            return
        if evt.action != 'created':
            return
        self.builder.push(evt.author, evt.safe_author, evt.repo, evt.id)


class IRCRebuildListener(events.EventTarget):
    """Listen for rebuild commands on IRC."""

    def __init__(self, builder):
        super(IRCRebuildListener, self).__init__()
        self.builder = builder

    def accept_event(self, evt):
        return evt.type == events.IRCMessage.TYPE

    def push_event(self, evt):
        trusted = 'o' in evt.modes
        if not evt.direct or not trusted:
            return
        matches = re.search(r'\brebuild (pr ?)?(?P<pr_id>\d+)\b', evt.what,
                            re.I)
        if not matches:
            return
        pr_id = matches.group('pr_id')
        try:
            pr_id = int(pr_id)
        except ValueError:
            return
        self.builder.push(evt.who, trusted, cfg.irc.rebuild_repo, pr_id)


class BuildStatusCollector:
    def __init__(self):
        self.queue = queue.Queue()

    def push(self, evt):
        self.queue.put(evt)

    def run(self):
        while True:
            evt = self.queue.get()
            builder = evt.builder.name
            props = utils.ObjectLike(
                {k: v[0] for k, v in evt.properties.items()})
            has_all_required = True
            for required in ('headrev', 'repo', 'shortrev'):
                if required not in props:
                    has_all_required = False
                    break
            if not has_all_required:
                continue
            headrev = props.headrev
            repo = props.repo
            pr_id = props.pr_id
            shortrev = props.shortrev
            pending = not evt.complete
            success = evt.results in (0, 1)  # SUCCESS/WARNING

            if builder in cfg.buildbot.pr_builders:
                if pending:
                    description = 'Auto build in progress on builder %s' % builder
                elif success:
                    description = 'Build succeeded on builder %s' % builder
                else:
                    description = 'Build failed on builder %s' % builder

                evt = events.BuildStatus(repo, headrev, shortrev, builder,
                                         pr_id, success, pending, evt.url,
                                         description)
                events.dispatcher.dispatch('buildbot', evt)
            elif pr_id and builder in cfg.buildbot.fifoci_builders and success:
                evt = events.PullRequestFifoCIStatus(repo, headrev, builder,
                                                     pr_id)
                events.dispatcher.dispatch('buildbot', evt)


class BBHookListener(events.EventTarget):
    def __init__(self, collector):
        super(BBHookListener, self).__init__()
        self.collector = collector

    def accept_event(self, evt):
        return evt.type == events.RawBBHook.TYPE

    def push_event(self, evt):
        self.collector.push(evt.raw)


def start():
    """Starts all the Buildbot related services."""

    pr_builder = PullRequestBuilder()
    events.dispatcher.register_target(PullRequestListener(pr_builder))
    events.dispatcher.register_target(ManualPullRequestListener(pr_builder))
    events.dispatcher.register_target(IRCRebuildListener(pr_builder))
    utils.DaemonThread(target=pr_builder.run).start()

    collector = BuildStatusCollector()
    events.dispatcher.register_target(BBHookListener(collector))
    utils.DaemonThread(target=collector.run).start()
