# CupX

**CupX is an original Xbox technical proof of concept for reconstructing a small, native Cuphead gameplay path.**

> [!IMPORTANT]
> **CupX is not the full game.** It is not a complete Cuphead port, replacement, or full-game release. The current build is a technical demonstration / vertical slice intended to prove the asset pipeline, native Xbox runtime, rendering, animation, audio, input, scene handling, and selected gameplay systems on original Xbox hardware.

The release contains the compiled Xbox application (`default.xbe`) and the **CupX Demo Data Builder**. It does **not** include Cuphead retail game data, and the CupX project source tree is not included in this release.

Cuphead and all associated game content, characters, artwork, audio, names, and trademarks belong to **Studio MDHR Entertainment Inc.** CupX is an unofficial homebrew project and is not affiliated with, sponsored by, or endorsed by Studio MDHR, Microsoft, Valve, or GOG.

---

## What the technical demo includes

The current demo focuses on a small, controlled route through the game rather than full-game coverage. The supported path includes:

- startup/title presentation and **Press Any Button** flow;
- slot-select / start-menu presentation;
- the opening book intro sequence;
- the Elder Kettle room and dialogue sequence;
- entry into the Tutorial;
- reconstructed player movement and selected player actions;
- shooting / weapon presentation;
- parry interactions;
- resurrection presentation;
- Tutorial coin behavior;
- Tutorial instructional text and control prompts;
- pause / exit-to-title behavior;
- music, sound effects, animation, scene composition, and supporting runtime systems used by the demo; and
- the CupX technical-demo end scene.

Large portions of Cuphead are intentionally **not** present. Bosses, the world map, shops, progression, complete save handling, the full weapon/super set, run-and-gun stages, DLC content, and general full-game completion are outside the scope of this release.

---

## Hardware / software requirements

### Xbox

- Original Microsoft Xbox capable of launching unsigned/homebrew XBE applications.
- The current technical demo is developed and tested primarily on a **128 MB upgraded Xbox**. A stock 64 MB Xbox is **not currently a supported target for this demo release**.

### PC — Demo Data Builder

- Windows 10 or newer.
- Python 3.10 or newer.
- A legally obtained PC installation of **Cuphead** containing `Cuphead.exe` and `Cuphead_Data`.
- Enough free disk space for the generated CUPX data set.

The Demo Data Builder automatically checks/installs the Python modules it requires, including PySide6, UnityPy, and Pillow. Microsoft DirectXTex `texconv.exe` is included with the builder where required for texture mastering; its license is included with the tool package.

---

## You must provide your own copy of Cuphead

**No Cuphead retail assets are distributed with CupX.**

The Demo Data Builder reads the data from a Cuphead installation that you already own and generates the native data files required by the Xbox technical demo. It does not download the game for you.

You can purchase the PC version from either of the following official storefronts:

- **Steam:** https://store.steampowered.com/app/268910/Cuphead/
- **GOG:** https://www.gog.com/en/game/cuphead

The current CupX demo targets **base-game content**. *Cuphead: The Delicious Last Course* is not required for this technical demo.

Generated `.cupx` files contain data derived from your local game installation. They are intended for your own use with CupX and should not be redistributed as a replacement for owning Cuphead.

---

## Using the CupX Demo Data Builder

The release includes a simplified builder specifically for the current technical-demo data set. You do **not** need the CupX development toolchain or Xbox source code to build the demo data.

### 1. Install Cuphead on your PC

Install a legitimate Steam or GOG copy of Cuphead. The selected folder should contain:

```text
Cuphead.exe
Cuphead_Data\
```

You may select either the folder containing those items or the `Cuphead_Data` folder itself; the builder can resolve the game root from either location.

### 2. Extract the Demo Data Builder

Extract the complete `CUPX_Demo_Data_Builder` folder to a normal writable location on your PC.

Do not run it from inside the Cuphead installation directory and do not place the output inside `Cuphead_Data`.

### 3. Launch the builder

Run:

```text
run_demo_builder.bat
```

The launcher checks the required Python environment and dependencies before opening the builder UI.

### 4. Select your Cuphead installation

Choose the PC Cuphead folder or the `Cuphead_Data` directory.

The builder scans and inventories the installation automatically. There is no separate developer inventory step in the demo builder.

### 5. Select an output directory

Choose an empty or dedicated output folder outside the Cuphead installation.

The builder replaces existing CupX-owned output files in that folder when a new demo data set is created.

### 6. Build the demo data

Click **Build Demo Data** and allow the process to finish completely.

A successful build produces the Xbox data set, including:

```text
cuphead.cupm
*.cupx
```

The builder also creates an `output\logs` directory containing diagnostic and verification reports. These reports are useful for troubleshooting but are **not** required on the Xbox.

### 7. Copy the complete data set to the Xbox

Create a folder for CupX on the Xbox and place the supplied XBE together with the complete generated data set.

> [!IMPORTANT]
> **All generated CupX data files must be placed in the same root folder as `default.xbe`.**
>
> Do **not** place `cuphead.cupm` or the generated `.cupx` volumes in a `data`, `assets`, `media`, or other subfolder. The Xbox runtime expects the manifest and package volumes beside the XBE.

Correct layout:

```text
CupX\
  default.xbe
  cuphead.cupm
  common_000.cupx
  frontend_*.cupx
  scene_*.cupx
  ...all other generated .cupx volumes...
```

Incorrect layout:

```text
CupX\
  default.xbe
  data\
    cuphead.cupm
    *.cupx
```

**Copy the entire generated data set into the same folder as `default.xbe`.** Do not copy only the files you believe changed unless you have independently verified every omitted volume is byte-identical. The manifest and CUPX volumes are generated as one matched set.

The builder's `logs` folder, JSON reports, and PC-side temporary files do not need to be copied to the Xbox.

### 8. Launch CupX

Launch `default.xbe` using your normal homebrew dashboard or application launcher.

---

## Builder troubleshooting

### The builder rejects the selected source

Confirm that you selected either:

```text
<game folder>\Cuphead.exe
<game folder>\Cuphead_Data\
```

or the `Cuphead_Data` folder itself.

### Python dependencies fail to install

Open a Command Prompt in the Demo Data Builder directory and run:

```text
py -3 -m pip install --upgrade -r requirements.txt
```

Then start `run_demo_builder.bat` again. Bootstrap failures are also written to `bootstrap_error.log` when available.

### The build fails

Check the selected output directory, particularly:

```text
logs\build_error.txt
logs\build.log
logs\cuphead.build_report.json
logs\cuphead.transfer_verify.json
```

The source-preflight reports in the same folder can identify missing or incompatible retail assets before the expensive packaging stages run.

### The Xbox build fails after copying data

Re-copy `cuphead.cupm` **and every generated `.cupx` volume** from the same completed build. Mixing files from separate builder runs can produce manifest, CRC, or asset-resolution failures.

---

## Light technical overview

CupX is a native original-Xbox application rather than a Unity runtime running on Xbox.

The Xbox side is implemented around the original Xbox hardware/software model, including RXDK-compatible C/C++, Direct3D 8 / NV2A rendering, XInput, DirectSound, and Xbox filesystem access. Cuphead-specific behavior is reconstructed only as required by the current technical demo.

The PC builder performs the expensive interpretation of the user's retail Unity data and converts the required content into Xbox-oriented native packages. The Xbox runtime then consumes those preprocessed packages rather than parsing Unity files directly.

The main container formats used by the demo are:

```text
CUPM  manifest / package map
CUPX  indexed asset volumes
CUPT  native texture payloads
CUPR  sprite descriptors
CUPA  animation data
CUPS  audio data
CUPN  native scene data
```

This separation is intentional: the PC does the general-purpose asset conversion work, while the Xbox receives a compact data set designed for the native runtime.

---

## Source data, references, and credits

### Cuphead / Studio MDHR

**Cuphead** was created by Studio MDHR Entertainment Inc. CupX would not exist without their game, artwork, animation, audio, and design.

- Studio MDHR: https://studiomdhr.com/
- Cuphead official site: https://cupheadgame.com/
- Steam: https://store.steampowered.com/app/268910/Cuphead/
- GOG: https://www.gog.com/en/game/cuphead

CupX obtains game assets only from the user's own installed PC copy. No retail Cuphead data is included in this release.

### Cuphead-Decomp

The project uses the **Cuphead-Decomp** project by **tanosshi** as an important behavioral, scene-layout, animation, timing, and game-logic reference while reconstructing the technical demo.

- Repository: https://github.com/tanosshi/Cuphead-Decomp
- Reference revision used extensively during this demo's development: https://github.com/tanosshi/Cuphead-Decomp/tree/96f1d23575cf94f87ec62736250b034cd1541712

Cuphead-Decomp is a reference project; it is not bundled as the CupX game runtime, and CupX does not distribute its copy of Cuphead assets.

### UnityPy

The Demo Data Builder uses **UnityPy** for reading and interpreting Unity asset data on the PC side.

- https://github.com/K0lb3/UnityPy

### Microsoft DirectXTex

The builder uses Microsoft's **DirectXTex / texconv** tooling where required for offline texture processing and block compression.

- https://github.com/microsoft/DirectXTex

DirectXTex is distributed under the MIT License. See the license included with the builder package.

### Qt for Python / PySide6

The Demo Data Builder user interface uses **PySide6 / Qt for Python**.

- https://doc.qt.io/qtforpython-6/

### Pillow

**Pillow** is used by the PC-side tooling for image-processing tasks required by the asset pipeline.

- https://python-pillow.github.io/

### Python

The Demo Data Builder runs on Python 3.

- https://www.python.org/

---

## Project status

CupX should be evaluated as a **technical proof of concept**. The purpose of this release is to demonstrate that a native original-Xbox runtime can ingest user-generated Cuphead data and reproduce a meaningful slice of the frontend, cutscene, room, Tutorial, rendering, animation, audio, and gameplay stack.

There will be inaccuracies, missing systems, performance work still to be done, and behavior that differs from the retail game. Those limitations are expected at this stage and should not be interpreted as full-game support.

---

## Distribution notice

This release intentionally does **not** bundle Cuphead retail data. Do not redistribute generated CUPX data sets containing content from a retail Cuphead installation.

CupX is an independent, unofficial homebrew project. All Cuphead-related copyrights and trademarks remain with their respective owners.
