#!/bin/bash

echo "starting to install all packages required for Bucket"

sudo apt-get -y update
sudo apt-get -y upgrade

sudo apt-get -y install python3-pip
pip3 install --upgrade setuptools
sudo apt-get -y install debhelper build-essential git
sudo apt-get -y install cpufrequtils

sudo apt-get -y install python-rpi.gpio python3-rpi.gpio
sudo apt-get -y install python3-gpiozero
sudo apt-get -y install i2c-tools libgpiod-dev
sudo apt-get -y install python3-smbus
sudo apt-get -y install python3-pil
sudo apt-get -y install python3-psutil
sudo apt-get -y install python3-pyftpdlib
pip3 install adafruit-circuitpython-ssd1306
sudo pip3 install adafruit-circuitpython-ssd1306
pip3 install pathlib
sudo pip3 install pathlib
pip3 install qrcode[pil]
sudo pip3 install qrcode[pil]

sudo apt-get -y install exfat-fuse exfat-utils
# the following might fail
sudo apt-get -y install usbmount

if [[ $? > 0 ]]
then
    echo "failed to install usbmount using apt-get, calling for alternative install"
    ./install_usbmount.sh
else
    echo "successfully installed usbmount using apt-get"
fi

pip3 install --upgrade adafruit-python-shell
sudo pip3 install --upgrade adafruit-python-shell
pip3 install --upgrade RPi.GPIO
sudo pip3 install --upgrade RPi.GPIO
pip3 install --upgrade adafruit-blinka
sudo pip3 install --upgrade adafruit-blinka

echo "finished all package installation tasks"
