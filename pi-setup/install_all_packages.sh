#!/bin/bash

echo "starting to install all packages required for Bucket"

sudo apt-get update
sudo apt-get upgrade

sudo apt-get -y install python3-pip
pip3 install --upgrade setuptools

sudo apt-get -y install python-rpi.gpio python3-rpi.gpio
sudo apt-get -y install python3-gpiozero
sudo apt-get -y install python3-smbus
sudo apt-get -y install python3-pil
sudo apt-get -y install python3-psutil
sudo apt-get -y install python3-pyftpdlib
pip3 install adafruit-circuitpython-ssd1306
pip3 install pathlib

echo "calling Adafruit's Blinka installer"

pip3 install --upgrade adafruit-python-shell
sudo pip3 install --upgrade adafruit-python-shell
wget https://raw.githubusercontent.com/adafruit/Raspberry-Pi-Installer-Scripts/master/raspi-blinka.py
sudo python3 raspi-blinka.py

echo "finished all package installation tasks"
