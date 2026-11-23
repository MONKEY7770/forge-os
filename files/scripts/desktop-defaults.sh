#!/usr/bin/env bash
# Forge OS desktop defaults ("Tempered"). Bazzite already ships /etc/xdg/kdeglobals (it locks the
# update page and sets fonts), so Forge only adds its own keys with KDE's kwriteconfig6 and never
# replaces those files. These are defaults: each person's own settings win, and nothing is locked.
set -euo pipefail

kw() {   # file group key value
  kwriteconfig6 --file "/etc/xdg/$1" --group "$2" --key "$3" "$4"
}

# Forge's own colours (light; the dark scheme is one click away in Settings).
kw kdeglobals General ColorScheme ForgeTempered
# IBM Plex fonts at the sizes Bazzite uses (Thai text falls back to IBM Plex Sans Thai, see
# /etc/fonts/conf.d/60-forge-plex.conf).
for key in font menuFont; do kw kdeglobals General "$key" "IBM Plex Sans,10,-1,5,50,0,0,0,0,0"; done
kw kdeglobals General toolBarFont "IBM Plex Sans,9,-1,5,50,0,0,0,0,0"
kw kdeglobals General smallestReadableFont "IBM Plex Sans,8,-1,5,50,0,0,0,0,0"
kw kdeglobals General fixed "IBM Plex Mono,10,-1,5,50,0,0,0,0,0"
# Slightly quicker, calmer animations everywhere (1.0 is KDE's default speed, 0 turns them off).
kw kdeglobals KDE AnimationDurationFactor 0.8
# Minimise into the dock with Magic Lamp instead of Squash (only one minimise effect can be on).
kw kwinrc Plugins magiclampEnabled true
kw kwinrc Plugins squashEnabled false

echo "desktop-defaults: Tempered colours, IBM Plex fonts, animation speed 0.8, Magic Lamp minimise"
