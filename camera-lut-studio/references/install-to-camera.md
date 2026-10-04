# Installing the product APK on the camera

## Before the first install

The camera needs Wi-Fi ADB, which is enabled from an app that must be installed
over USB:

1. Set the camera's USB mode to **MTP** and connect it.
2. Use [pmca-gui](https://github.com/ma1co/Sony-PMCA-RE) (Sony-PMCA-RE) → *Install
   app* → select **OpenMemories: Tweak** → *Install selected app*.
3. Disconnect USB, open **OpenMemories: Tweak** on the camera, configure a Wi-Fi
   access point, then in the **Developer** page enable **Enable Wifi** and
   **Enable ADB**. Note the IP the camera shows.
4. Only ADB is needed. Do not enable Telnet, unlock settings protection, change the
   region, or touch the firmware.

## adb

`adb` is not on `PATH` on this machine. It lives in the WorkBuddy runtime dir:

```
~/.workbuddy/binaries/platform-tools/versions/37.0.1/adb.exe
```

Locate it generically by reading `~/.workbuddy/binaries/platform-tools/versions/current`.

## Install

Uninstall any previous build of the same package first. Every locally signed build
uses its own key, and Android refuses to replace an app whose signing certificate
differs - over ADB this surfaces as `INSTALL_FAILED_UPDATE_INCOMPATIBLE`, and over
the USB installer as the generic `Communication error 100`. Removing the old app
clears both. The package name is always
`com.yuki.imaging.app.pictureeffectplus`.

```sh
adb connect CAMERA_IP:5555
adb devices                                  # expect: device
adb -s CAMERA_IP:5555 install -r /path/to/FilmStudio-UserCustomize.apk
```

Keep the signing key (`~/.workbuddy/camera-lut-studio/signing.pem` by default).
Reusing one key lets later builds install with `-r` and no uninstall.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Communication error 100: Error completed` from pmca-gui | The camera's installer returned a generic failure. **Confirmed cause: a same-package app is already installed** - uninstall the old build, then retry. Upstream also documents this code as the stock `scalarainstaller` rejecting a non-Sony signature. Renaming the APK file has nothing to do with it (compare SHA-256 to be sure). |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | Different signing key. Reuse the original key, or uninstall the old app first. |
| `INSTALL_PARSE_FAILED_NO_CERTIFICATES` | The APK was re-signed with a modern signer. The camera runs Android 4.1.2 and its Harmony runtime cannot parse modern CMS protected attributes; sign with `-noattr -binary` (which `inject_lut.py` does). |
| `INSTALL_FAILED_DEXOPT`, log shows `Bogus method access flags` | A non-native method carries `ACC_SYNCHRONIZED (0x20)`. Use `monitor-enter`/`monitor-exit` in smali instead. |
| installs fine, menu entry missing | Check `assets/MenuData.xml` in the APK: the entry must be present and use the same node form as its siblings. |
| installs fine, entry shows a wrong name (e.g. 流行色彩) and changes no colour | The lookup returns null - the injected branch is unreachable or its condition polarity is inverted. See `references/color-model.md` traps 1 and 2. |
| works but the look is very weak | Compare against the built-in Fujifilm presets; a midtone offset near +0 is a very mild LUT. Strengthen the curve and matrix in the designer. |
| colours look wrong after exiting the app | Exit the app normally and reboot the camera. Do not change firmware or factory-reset to debug this app. |

Finish a session with:

```sh
adb disconnect CAMERA_IP:5555
```

and turn ADB off again in Tweak. The disconnect only drops the PC side; it does not
stop the daemon on the camera.

## Known limits

- Still unverified on hardware: performance smoothness and colour restoration after
  exiting the app. The upstream project validated install, launch and live filter
  switching on the A6000; the A5100 has the deeper test history.
- A 3x3 matrix plus one common gamma curve cannot reproduce hue-dependent 3D LUT
  behaviour, grain, or sensor response.
- Keep the total preset count moderate. Each preset adds four strength variants of
  9 matrix values plus 2048 gamma bytes to the class initialiser.
