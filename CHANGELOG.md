# Changelog

## 2.1.0 — 2026-08-11

WorkBuddy runtime support and two renderer additions. The data contract, validation, and merge behavior are unchanged.

WorkBuddy 运行环境适配，外加两处渲染增强。数据契约、校验与合并行为不变。

- `install --target workbuddy` installs to `$HOME/.workbuddy/skills`, with the same conflict refusal, hash verification, and `--force` backup behavior as the other targets. Verified against WorkBuddy's bundled Python 3.13.12 on macOS; see [agent compatibility](references/agent-compatibility.md) for the exact claim boundary.
- The dashboard now renders `project.milestones` as a card grid (due date plus name) above the task section, respecting per-milestone audience visibility.
- A task with no resolvable owner now shows an explicit Unassigned (待指派) label instead of omitting the owner line.
- `SKILL.md` description gains a Chinese trigger sentence so Chinese-first clients match Chinese requests reliably.

## 2.0.0 — 2026-08-11

Renderer overhaul. The canonical data contract, CLI flags, validation, and merge behavior are unchanged; the rendered dashboard, brief, and theme preview are redesigned. The HTML structure and class names changed, which is why this is a major version.

渲染层整体重做。数据契约、CLI、校验与合并行为不变；看板页面、brief 摘要、主题预览全部重新设计。HTML 结构与 class 名有变，因此升主版本号。

- Editorial design system: hero header with serif display title, status stat band, numbered workflow steps with human-gate badges, dot timeline, color-coded status/decision/result pills, member avatar cards, and a deliveries section.
- Bilingual output: `meta.language` starting with `zh` renders the dashboard and brief with Chinese labels; other languages render English. Audience JSON stays machine-readable English.
- Four retuned themes with per-theme status colors: `warm` (flagship: cream canvas, single orange accent, warm shadows), `clean`, `dark`, `paper`. Print resets to black on white.
- Typography: local system font stack with CJK coverage, serif display for headings and numerals, no remote assets, no fixed pixel widths.
- Responsive: two-column stat grid and single-column sections under 40rem; verified at exact 1280 and 390 viewport widths with no horizontal overflow.

## 1.0.0 — 2026-08-11

Initial release.
