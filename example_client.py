#!/usr/bin/env python3
import socket
import os
import time

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

def main():
    with open(os.path.expanduser('~/.windows-hotkey-server'), 'rb') as f:
        auth = f.read().strip()

    should_switch = True
    last_mouse_y = 0
    while True:
        mouse_x, mouse_y = get_mouse_pos()
        if mouse_x <= 0:
            if should_switch:
                switch(auth, mouse_y)
            should_switch = False
        else:
            should_switch = True
        time.sleep(0.05)

if __name__ == '__main__':
    main()
