CUPX production BC texture backend

CUPX uses Microsoft DirectXTex texconv for retail BC7 -> Xbox DXT conversion.
Pinned release: may2026
URL: https://github.com/microsoft/DirectXTex/releases/download/may2026/texconv.exe
SHA-256: dcfdec10244e02cf5037fba089c55fb7e1326b1c8181742d77d15fa5cb5eef06

If tools/texconv.exe is absent, CUPX downloads and verifies it on first BC conversion.
Set CUPX_TEXCONV to use an existing compatible texconv executable instead.
