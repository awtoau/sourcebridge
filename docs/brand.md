# Sourcebridge brand

Sourcebridge has an original identity built around a simple idea: a bridge
connects two systems, while a marked centre point shows that the connection is
supported by evidence. It is deliberately language- and target-neutral.

The artwork takes only broad local visual cues—clear industrial geometry, a
deep ink base, and a confident blue accent. It does not reuse any existing
awto artwork, gear forms, wordmark, or layout.

## Files

- [`assets/sourcebridge-logo.svg`](../assets/sourcebridge-logo.svg) is the
  horizontal logo for README banners, social headers, and documentation.
- [`assets/sourcebridge-icon.svg`](../assets/sourcebridge-icon.svg) is the
  square icon for repository avatars and small surfaces.

## Colours

| Name | Hex | Use |
| --- | --- | --- |
| Bridge ink | `#10233F` | Wordmark, icon field, primary text |
| Bridge blue | `#2E72D2` | Bridge structure and primary accent |
| Signal blue | `#78A8EE` | Connections and secondary accent |
| Evidence mint | `#58D6BA` | Verified evidence point only |
| Paper | `#F7FAFC` | Light background and icon nodes |
| Slate | `#42617F` | Supporting text on light backgrounds |

## Clear space and sizing

Keep clear space equal to the width of either endpoint node around every edge
of the logo or icon. Do not place text, rules, or other marks in that area.

Use the horizontal logo at 240 px wide or larger. Use the square icon at 32 px
or larger; below 48 px, prefer the icon rather than the full wordmark. Keep
the supplied proportions, colours, and bridge/evidence relationship intact.
Do not add shadows, gradients, outlines, substitute type, or decorative
symbols.

## Backgrounds

On light backgrounds, use the horizontal SVG as supplied. On dark backgrounds,
the same SVG adapts in supporting viewers through `prefers-color-scheme`; when
that is not available, place the full-colour square icon on the dark surface
and use the `Sourcebridge` name in surrounding accessible text. Maintain
strong contrast and do not put the transparent logo over busy imagery.

The icon has its own deep-ink field and is suitable on both light and dark
surfaces without alteration.

## Alt text

Use this concise alt text for the horizontal mark:

> Sourcebridge — conversion with evidence.

Use this concise alt text for the icon:

> Sourcebridge icon: a bridge connecting two nodes through a verified evidence point.

When the adjacent visible text already says “Sourcebridge”, use an empty alt
attribute for a decorative duplicate mark.
