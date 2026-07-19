# Thumbnail Fonts

These font files are required by the Pillow thumbnail overlay (ADR-0034 §(3),
§(6)). The binaries are **bundled** (T050): each `*-Regular.ttf` below plus its
`*-OFL.txt` license is committed in this directory. Variable-weight upstreams
(Playfair Display / Space Grotesk / Noto Sans JP / Cormorant Garamond) are saved
under the `-Regular.ttf` candidate name the overlay resolves; Pillow loads them
via the default instance.

All fonts below are licensed under the **SIL Open Font License 1.1**, which
permits commercial use, redistribution, and bundling without modification.

## Required fonts

| Font | Used by genre(s) | License | Source |
|---|---|---|---|
| Bebas Neue | lo-fi hip-hop, chillhop | SIL OFL 1.1 | https://fonts.google.com/specimen/Bebas+Neue · https://github.com/dharmatype/Bebas-Neue |
| Cormorant Garamond | ambient | SIL OFL 1.1 | https://fonts.google.com/specimen/Cormorant+Garamond · https://github.com/CatharsisFonts/Cormorant |
| VT323 | synthwave | SIL OFL 1.1 | https://fonts.google.com/specimen/VT323 · https://github.com/phoikoi/VT323 |
| Playfair Display | piano solo | SIL OFL 1.1 | https://fonts.google.com/specimen/Playfair+Display · https://github.com/clauseggers/Playfair-Display |
| Space Grotesk | future garage | SIL OFL 1.1 | https://fonts.google.com/specimen/Space+Grotesk · https://github.com/floriankarsten/space-grotesk |
| Noto Sans JP | all genres (Japanese headline) | SIL OFL 1.1 | https://fonts.google.com/noto/specimen/Noto+Sans+JP · https://github.com/notofonts/noto-cjk |

## Notes

- The genre → `font_primary` mapping is defined in
  `backend/templates/thumbnail/<genre>.yaml`. `font_secondary` is always
  Noto Sans JP for Japanese glyph coverage.
- When adding the binaries (T050), place each `.ttf`/`.otf` in this directory
  and keep the accompanying `OFL.txt` license file from each upstream project,
  as required by the SIL OFL.
- Render output differs slightly across OS font rendering; the baseline is an
  Ubuntu (host) sample as decided in ADR-0034 ("受容したリスク").
