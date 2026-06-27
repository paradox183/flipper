# Flipper

Flipper pulls realtime race data from the Dolphin timing software, enabling alternative scoreboard uses.  It reads data directly off of the USB Dolphin receiver during a race.

## Prerequisites

* Latest version of Dolphin v5 software installed
* Python is installed and added to Windows PATH
* Wireshark with USBPcap installed
* Enable TCP/IP streaming in the Dolphin software; open specified port on Windows Firewall
* Meet settings linked from Meet Maestro

## Setup

1. Put dolphin_scoreboard.py, dolphin_usb_decoder.py, and scoreboard.ini in a folder of their own.
2. Setup Dolphin as you normally would for a meet: connect Dolphin USB base unit, launch Dolphin software, point to a data folder.
3. Complete the Timing Setup steps in Meet Maestro, OR reuse a data file (usually event_settings_v01.csv) from a previous meet for testing/demo purposes.
4. Enable TCP/IP streaming in the Dolphin software (Settings screen, click "TCP/IP" and it changes to "ON").  Note the port number, or click the port number to cycle through various port options.
5. Launch Wireshark and determine which USBPcap device the base unit appears under.  In my case this is USBPcap1, but this may depend on your system and possibly which physical USB port you use.
6. Edit the scoreboard.ini folder to suit your needs/preferences and the folder path for your specific meet.
7. Launch Flipper from the command line (accept the Windows UAC prompt which grants USBPcap permission to watch the USB traffic): `python dolphin_scoreboard.py`

## How it works

Flipper watches the data between the software and the Dolphin base unit to understand the timing system's state at any given time.  It also uses the TCP/IP stream to know the current event/heat numbers in Dolphin, and the event settings data file to know event names and how many heats per event.  When it detects that a race has started, it immediately starts a race clock on the web GUI.  This clock runs in parallel with the Dolphin software and is therefore not "official", however it is close enough for use as a visual aid on a scoreboard.

As Dolphin watches are stopped, Flipper reads the official time from the USB traffic and shows it on the screen.  Results are computed the same way Dolphin and Meet Maestro operate:
* For one time, use that time
* For two times, take the average
* For three times, take the median

It then applies the pool conversion specified in scoreboard.ini.

The race clock keeps running until the Dolphin software is reset.  At that time, the race clock stops and the next event/heat is shown on screen.  The race results and event/heat number are held on the screen until the next race starts, at which point the times are cleared and the race clock starts from zero.

There is one quirk to the event/heat numbers.  As you know, it is possible to change event/heat during a race, and Dolphin automatically advances event/heat as necessary upon reset.  Flipper cannot take the current event/heat at face value; otherwise, once the race is reset it will appear to show events for the upcoming race, not the race that was just run.  To work around this, Flipper waits for one second after the event/heat numbers change before displaying that in the web GUI.  This seems to adequately accept manual changes while rejecting the automatic advance upon reset.

## scoreboard.ini Settings

* `usb_iface` - the USB interface to which the Dolphin base unit is connected
* `dolphin_host` - the IP address of the computer running the Dolphin software; most likely 127.0.0.1
* `dolphin_port` - matches the port number in the Dolphin TCP/IP settings
* `lanes` - number of lanes to show on the web GUI
* `conversion_factor` - the decimal pool conversion factor applicable to your pool
* `http_port` - the port on which the web GUI operates
* `event_settings_folder` - usually the path to the data folder for the current meet
* `event_settings_filename` - the data file containing event names, events, and heats; usually comes from Meet Maestro as `event_settings_v01.csv`

## Current limitations

* Only knows events and heats as of the start of the race.  Any changes during the race are not reflected.

## Potential improvements / to-dos

* Read data directly from Meet Maestro: events and heats, swimmer names, etc.
* Combined heat logic
* ???

## Disclaimer

This was done completely with Claude Opus.  I am not a developer.
