#! /bin/bash

if [[ $1 == "version" ]]
then
    dnf info $2 | grep Version | cut -d":" -f2 | head -n1
    if [[ $2 == "rocm-meta" ]]; then
        echo "These are OPTIONAL additional packages for OpenCL/ROCm support"
    elif [[ $2 == "cuda-devel" ]]; then
        echo "These are OPTIONAL additional packages for CUDA support"
    elif [[ $2 == "mesa-vulkan-drivers-git" ]]; then
        echo "These are OPTIONAL ALTERNATIVE AMD + Intel Vulkan drivers which are built from open-source Mesa git repository frequently. They may contain fixes (or also bugs) which are not present in the current stable release."
    fi
elif [[ $1 == "description" ]]
then
        #apt-cache show $2 | grep 'Description*' | cut -d":" -f2 | head -n1
        dnf info $2 | sed -n '/Description/,/^$/p' | awk 'NR==1,/^$/ {if (/^$/ && printed) exit; if (NF) printed=1; if (NR==1) sub(/Description *: */, ""); else sub(/^ *: */, ""); print}'
fi
