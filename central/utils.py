"""Utility functions and classes."""

from Crypto.Cipher import AES

import base64
import json
import hashlib
import logging
import os
import requests
import threading
import time


def shorten_url(url):
    """Minify a URL using dolp.in."""
    if url == '':
        return '<no url>'
    elif url.startswith("https://github.com/dolphin-emu/dolphin/pull/"):
        return url.replace("https://github.com/dolphin-emu/dolphin/pull/", "https://dolp.in/pr")
    elif url.startswith("https://github.com/dolphin-emu/dolphin/commit/"):
        return url.replace("https://github.com/dolphin-emu/dolphin/commit/", "https://dolp.in/r")
    return url


class DaemonThread(threading.Thread):
    daemon = True

    def __init__(self, *args, **kwargs):
        super(DaemonThread, self).__init__(*args, **kwargs)
        self.daemon_target = kwargs.get('target')
        self.args = kwargs.get('args', ())
        self.kwargs = kwargs.get('kwargs', {})
        if self.daemon_target is None:
            self.daemon_target = self.run_daemonized

    def run(self):
        while True:
            try:
                print('Running %s' % self.daemon_target)
                self.daemon_target(*self.args, **self.kwargs)
            except Exception:
                logging.exception('Daemon thread %r failed', self)
                time.sleep(1)


class ObjectLike:
    """Transforms a dict-like structure into an object-like structure."""

    def __init__(self, dictlike):
        self.reset(dictlike)

    def reset(self, dictlike):
        self.dictlike = dictlike

    def items(self):
        for k, v in self.dictlike.items():
            if isinstance(v, dict):
                yield (k, ObjectLike(v))
            else:
                yield (k, v)

    def __getattr__(self, name):
        val = self.dictlike.get(name)
        if isinstance(val, dict):
            return ObjectLike(val)
        else:
            return val

    def __contains__(self, name):
        return name in self.dictlike

    def __str__(self):
        return str(self.dictlike)

    def __repr__(self):
        return repr(self.dictlike)


def spawn_periodic_task(interval, f, *args, **kwargs):
    def wrapper():
        while True:
            try:
                f(*args, **kwargs)
            except Exception:
                logging.exception('Periodic task %s failed', f.__name__)
            time.sleep(interval)

    DaemonThread(target=wrapper).start()


def encrypt_data(data, key):
    key = hashlib.sha1(key.encode('ascii')).digest()[:16]
    iv = os.urandom(16)
    aes = AES.new(key, AES.MODE_CBC, iv)
    length = len(data)
    if length % 16 != 0:
        data += b'\x00' * (16 - (length % 16))
    cipher = aes.encrypt(data)
    out = str(length).encode('ascii') + b'.'
    out += base64.b64encode(iv) + b'.'
    out += base64.b64encode(cipher)
    return out.decode('ascii')


def decrypt_data(data, key):
    key = hashlib.sha1(key.encode('ascii')).digest()[:16]
    length, iv, cipher = data.split(b'.', 3)
    length = int(length.decode('ascii'))
    iv = base64.b64decode(iv)
    cipher = base64.b64decode(cipher)
    aes = AES.new(key, AES.MODE_CBC, iv)
    return aes.decrypt(cipher)[:length].decode('ascii')
