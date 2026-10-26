# Pairing QR helper

`pair.cjs` turns an instance's pairing details (from
`./instances.sh _pair-info NAME`) into a connection link and terminal QR code
for a phone client. It was part of the retired npm CLI and is kept for the
Android client; it is not installed with the server.

`qr-renderer.cjs` is generated from qrcode 1.5.4; see `QR-NOTICES.txt` for its
license. Run the tests with `node --test tools/pairing/pair.test.cjs`.
