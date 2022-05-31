Software Setup Instructions:

NOTE: I am not providing instructions for the hardware build online. These instructions are meant for individuals who have obtained hardware from me personally, or has the skills to build the hardware without my help.

1. Burn a microSD card with whatever is the latest Raspbian operating system, use a 64 bit headless version (Lite version). I am using Buster Lite.

2. Do the usual raspi-conf steps, such as setting the password, expand file-system, enable SSH and enable I2C. The next few steps depends on the Raspberry Pi being connected to your home network and the internet via ethernet. Do not use Wi-Fi.

3. SSH into the Pi.

4. Follow official instructions on setting up the Raspberry Pi as a Wi-Fi access point. The hostapd.conf file I used is provided as an example as well.

5. Execute the script I've included named `install_all_packages.sh`, if something goes wrong, handle the problem accordingly. I cannot anticipate the changes to the package repositories so I cannot automate the handling of these problems. If the script doesn't have execution flags set, then use `chmod` to set the `+x` flag for it.

6. Use git to clone this repo, `git clone https://github.com/frank26080115/bucket.git`.

7. Edit the files `/etc/rc.local` and `/etc/usbmount/usbmount.conf`, I've included examples in this git repo directory

8. Reboot, fool around, test, change my scripts if you need to. If you are happy with it then put the microSD card in read-only mode.
