# CUPX Demo Data Builder

A simplified user-facing packer for the current CUPX original-Xbox technical demo.

It builds the supported route used by the current Xbox application:

- title and slot-select presentation;
- intro cutscene;
- Elder Kettle room and dialogue resources;
- Tutorial scene, player, weapon, effects, music, and native Tutorial metadata; and
- the data required to reach the CUPX technical-demo end scene.

This package does **not** contain Cuphead game data. You must provide a legally obtained PC installation of Cuphead.

## Requirements

- Windows 10 or newer
- Python 3.10 or newer
- A PC installation of Cuphead containing `Cuphead.exe` and `Cuphead_Data`
- Sufficient free disk space for the generated CUPX volumes

The launcher automatically installs or updates the required Python modules:

- PySide6
- UnityPy
- Pillow

Microsoft DirectXTex `texconv.exe` is already included and SHA-256 verified by the packer when it is used. Its license is included in the `tools` folder.

## Use

1. Extract the entire `CUPX_Demo_Data_Builder` folder.
2. Double-click `run_demo_builder.bat`.
3. Select the Cuphead game folder. You may also select `Cuphead_Data` directly; the builder will use its parent folder.
4. The builder scans and inventories the installation automatically. There is no separate inventory step.
5. Select an output directory. An empty folder outside the Cuphead installation is recommended.
6. Click **Build Demo Data**.
7. Wait for the progress bar to reach 100% and for the completion message.

## Generated files

The output directory contains the Xbox dataset:

- `cuphead.cupm`
- every generated `*.cupx` volume

Copy **the complete generated set** to the Xbox. Do not update only the volumes you expect to have changed unless you have independently verified that every omitted file is byte-identical.

The output `logs` folder contains:

- `build.log`
- `build_summary.json`
- `inventory.json`
- `inventory_summary.json`
- `cuphead.catalog.json`
- `cuphead.build_report.json`
- `cuphead.transfer_verify.json`
- source preflight reports
- `build_error.txt` when a build fails

These reports are not required on the Xbox.

## Notes

- The builder intentionally exposes no diagnostic-pack, full-game, worker-count, strict-mode, or media-test controls.
- The user-facing **Build Demo Data** action uses CUPX's deterministic demo/vertical-slice profile.
- Existing CUPX-owned `.cupx` files and `cuphead.cupm` in the selected output directory are replaced by a new build.
- The complete scan is kept in memory and written to `output/logs`; it is not displayed as a developer inventory table.

## Troubleshooting

### The source is rejected

Select the folder containing both `Cuphead.exe` and `Cuphead_Data`, or select the `Cuphead_Data` folder itself.

### Python modules fail to install

Run the following from a Command Prompt in this folder:

```text
py -3 -m pip install --upgrade -r requirements.txt
```

Then run `run_demo_builder.bat` again. Bootstrap failures are written to `bootstrap_error.log`.

### The build fails

Open the selected output directory and review:

```text
logs\build_error.txt
logs\build.log
logs\cuphead.build_report.json
```

The source preflight reports in the same folder identify missing or incompatible retail assets before the expensive packaging passes begin.

## Included third-party tool

`tools/texconv.exe` is part of Microsoft DirectXTex and is distributed under the included MIT license.
