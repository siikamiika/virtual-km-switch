#!/usr/bin/env python3

from virtual_km_switch import VirtualKMSwitch, create_key_event, create_rel_event, ecodes

def main():
    kbd_path = '/dev/input/by-path/platform-i8042-serio-0-event-kbd'
    # mouse_path = '/dev/input/by-id/usb-VMware_VMware_Virtual_USB_Mouse-mouse'
    km_switch = VirtualKMSwitch(kbd_path)

    km_switch.add_virtual_device_group(ecodes.KEY_F1, 'linux')

    def _handle_key_kp4(event):
        # keydown only
        if event.value != 1:
            return
        event.code = ecodes.REL_HWHEEL
        event.type = ecodes.EV_REL
        event.value = -1
        km_switch.route_event(event)
    km_switch.add_callback((ecodes.EV_KEY, ecodes.KEY_KP4), _handle_key_kp4)

    def _handle_key_kp6(event):
        # keydown only
        if event.value != 1:
            return
        event.code = ecodes.REL_HWHEEL
        event.type = ecodes.EV_REL
        event.value = 1
        km_switch.route_event(event)
    km_switch.add_callback((ecodes.EV_KEY, ecodes.KEY_KP6), _handle_key_kp6)

    km_switch.set_active(ecodes.KEY_F1)

    km_switch.start_loop()

if __name__ == '__main__':
    main()
