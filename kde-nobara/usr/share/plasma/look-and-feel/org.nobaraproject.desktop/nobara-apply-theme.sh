#!/bin/bash

# Check if 'name=Nobara' is present in the plasmarc file
if grep -q 'name=Nobara' ~/.config/kdedefaults/plasmarc; then
    # Check if '1' is not present in the nobara-theme-first-apply file
    if ! grep -q '1' ~/.config/nobara-theme-first-apply; then
        # Apply the Nobara theme
        gsettings set org.gnome.desktop.interface gtk-theme Nobara
        exec pkexec "/usr/bin/papirus-folders" "-C" "violet" "--theme" "Papirus-Dark"

        # Check if the breezerc file does not exist and copy it if necessary
        if [[ ! -f ~/.config/breezerc ]]; then
            cp /etc/xdg/breezerc ~/.config/
        fi

        # Capture the output of the grep command
        output=$(grep -rni ~/.config/ -e 'papirus-colors-dark')

        # Check if the output is not empty
        if [[ -n $output ]]; then
            # Use a while loop to iterate through each line of the output
            while IFS= read -r line; do
                file=$(echo "$line" | cut -d ":" -f 1)
                sed -i 's|Papirus-Dark|Papirus-Dark|g' "$file"
            done <<< "$output"
        fi

        # Mark the theme as applied
        echo "1" > ~/.config/nobara-theme-first-apply
    fi
fi
