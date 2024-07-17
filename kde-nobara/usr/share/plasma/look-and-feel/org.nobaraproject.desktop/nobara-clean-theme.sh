#!/bin/bash
if [[ -z $(cat ~/.config/kdedefaults/plasmarc | grep 'name=Nobara') ]]; then
    if [[ -f ~/.config/nobara-theme-first-apply ]]; then
        rm ~/.config/nobara-theme-first-apply
    fi
fi
