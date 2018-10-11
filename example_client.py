#!/usr/bin/env python3
import socket
import os
import time
import subprocess

from Xlib import display

root = display.Display().screen().root
def get_mouse_pos():
    data = root.query_pointer()._data
    return data["root_x"], data["root_y"]

def switch(auth, mouse_y):
    sock = socket.socket()
    sock.connect(('127.0.0.1', 9898))
    sock.send(auth + b'\n')
    sock.send(f'windows {mouse_y}\n'.encode('utf-8'))

def qemu_started():
    return bool(subprocess.getoutput('pgrep -f qemu-system-x86_64'))

def main():
    with open(os.path.expanduser('~/.windows-hotkey-server'), 'rb') as f:
        auth = f.read().strip()
    # last differing position
    last_pos = (0, 0)
    while True:
        current_pos = get_mouse_pos()
        # switch if we are at the edge and the mouse has moved
        if current_pos != last_pos:
            if current_pos[0] <= 0 and qemu_started():
                switch(auth, current_pos[1])
            last_pos = current_pos
        time.sleep(0.05)

if __name__ == '__main__':
    main()
