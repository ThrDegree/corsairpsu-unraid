## Introduction
This plugin is a wrapper for [corsairmi](https://github.com/notaz/corsairmi) which reads out monitoring information for Corsair RMi and HXi power supplies. It is now also a wrapper for [cpsumon](https://github.com/audiohacked/cpsumon) for AXi PSUs.

Originally created by Realies, Modified by Fma965 for more accurate statistics, to match with the unraid dashboard styling and to add AXi support.\
In this repository changes were made to get this plugin to work on UnRAID v.6.12.0-beta7 and above.

AX1600i support added by bngoold and tylerburns, using a Python-based USB monitor that communicates with the PSU directly (matching how iCUE reads data), with per-PCIe-connector 12V current and power display.

![Screenshot](https://i.imgur.com/Nq1dvW5.png)
![Screenshot](https://i.imgur.com/mSqSWdF.png)


## Usage
Plugins > Install Plugin (or it can be installed from the Community Apps plugin)
```
https://raw.githubusercontent.com/giganode/corsairpsu-unraid/master/corsairpsu.plg
```

## AX1600i Requirements

The AX1600i PSU type requires the **[Python 3 for Unraid](https://forums.unraid.net/topic/88014-plugin-python3/)** plugin and the `pyusb` package.

1. Install the **Python 3** plugin from Community Apps.
2. Place `ax1600i_monitor.py` at `/mnt/user/appdata/corsairPSU/ax1600i_monitor.py`.
3. Open the Python 3 plugin's **Auto-execution Script** editor and add:
   ```bash
   pip3 install pyusb -q
   python3 /mnt/user/appdata/corsairPSU/ax1600i_monitor.py --daemon &
   ```
4. Reboot (or run the above commands manually once from a terminal to start immediately).
5. Select **AX1600i** as the PSU type in the Corsair PSU plugin settings.

The daemon holds the USB connection open and writes readings to `/tmp/corsairpsu_ax1600i.json` every 2 seconds. The dashboard reads from that file, so no USB reset occurs on each refresh. The daemon restarts automatically after a reboot via the autoexec.sh script.
