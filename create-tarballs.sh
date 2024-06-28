#!/bin/bash
tar -cvzf kde-nobara.tar.gz kde-nobara
tar -cvzf nobara-drivers-gtk4.tar.gz nobara-drivers-gtk4
tar -cvzf nobara-nvidia-wizard.tar.gz nobara-nvidia-wizard
tar -cvzf nobara-resolve-runtime.tar.gz nobara-resolve-runtime
tar -cvzf nobara-logos.tar.gz nobara-logos
tar -cvzf nobara-welcome-gtk4.tar.gz nobara-welcome-gtk4
tar -cvzf nobara-updater.tar.gz nobara-updater

cd nobara-welcome/38
tar -cvzf nobara-welcome-38.tar.gz nobara-welcome
mv nobara-welcome-38.tar.gz ../../
cd ../../
cd nobara-welcome/39
tar -cvzf nobara-welcome-39.tar.gz nobara-welcome
mv nobara-welcome-39.tar.gz ../../
cd ../../
