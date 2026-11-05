# Roadmap

These are ordered by what blocks real games first.

1. **GX renderer backend** (OpenGL 3.3 / GLES 3 first). Implement `GXBackend`:
   - vertex decoding (VAT formats, dequantisation, indexed arrays from CP array bases)
   - XF transform/lighting state as uniforms
   - TEV stages generated as shaders
   - texture decoding (I4/I8/IA4/IA8/RGB565/RGB5A3/RGBA8/CMPR/C4/C8/C14X2 + TLUT)
   - EFB copies to textures and to the XFB
2. **DSP audio**: HLE of the AX / AXFX / Zelda ucodes (mailbox protocol + voice mixing) feeding
   `gcrt_platform_audio_push`. Also AI streaming (DTK) for games that stream ADPCM from the disc.
3. **Signature-based SDK identification** for stripped games, matching function bodies with
   relocations masked. This would apply HLE (`VIWaitForRetrace`, `OSGetTime`, `DVDRead*`,
   `CARD*`) automatically without a symbol map.
4. **Memory cards** (EXI slot A/B backed by `.raw` files) and RTC/SRAM.
5. **Wii**: fuller IOS HLE (ES, FS, `/dev/usb` for Wii Remotes through the host Bluetooth stack or
   a controller mapping, SD, NAND save files).
6. Performance: keep hot GPRs in C locals across straight-line code (a local register cache), elide
   redundant CR updates, and do partial inlining across small leaf functions.
7. Per-game patch hooks (widescreen, 60 fps, input remapping) applied at the C level.
