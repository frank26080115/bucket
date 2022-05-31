#!/bin/bash

echo "there's a chance that there will be errors, please read the errors and handle them"

echo "the errors cannot be handled automatically because the package repository is constantly being updated"

git clone https://github.com/rbrito/usbmount.git
cd usbmount
sudo dpkg-buildpackage -us -uc -b
cd ..
sudo dpkg -I usbmount_0.0.24_all.deb
