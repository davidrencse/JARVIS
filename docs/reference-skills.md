# Reference skills catalogue

*Twelve third-party agent skills plus four Three.js skills were once vendored under `skills/` as read-only reference text. The copies were removed in the 2026-11-13 docs cleanup (they were never runtime dependencies); this file keeps what is needed to get any of them back.*

## Restore

- From this repo: `git show <last-commit-with-skills>:skills/<skill>/SKILL.md`, or `git checkout <commit> -- skills/<skill>` (history before the cleanup commit has all files; `git log --diff-filter=D -- skills` finds it).
- From upstream: fetch the pinned commit below (GitHub contents API / `gh api`, since the Skills installer endpoints were unreachable from the original environment), then compare against `skills/sources.json`, which is kept and records repository, commit, source path and SHA-256 of every file.
- Global registration (optional, from a normal terminal; installs the *current* upstream, not the pin): `npx skills add <repo>@<skill> -g -y`, e.g. `npx skills add wondelai/skills@system-design -g -y`.
- Licences: MIT, except `webapp-testing` (Apache-2.0). `react-best-practices` and `composition-patterns` declare MIT in frontmatter but upstream has no LICENSE file at the pinned commit.
- Omitted when vendored (executable upstream files): `webapp-testing/scripts/with_server.py` and `examples/*.py`, `shadcn/scripts/showcase-coverage.mjs`. The shadcn SKILL.md has a ``!`npx shadcn@latest info --json` `` context-injection line that would run the CLI if registered with an agent that supports it.
- `frontend-design` was never vendored (installed globally). `performance-optimization` links to `../../references/performance-checklist.md`, which was not copied.

## Architecture skills (selected 2026-10-21)

| Skill | One-line purpose | Upstream pin |
| --- | --- | --- |
| system-design | Requirements, capacity estimates, reliability and architecture trade-offs. | `wondelai/skills` @ `c172996495bed0fcd26896a9416b2093fd7073f0`, path `system-design` |
| architecture-patterns | Module boundaries, dependency direction, testable adapters (Python examples). | `wshobson/agents` @ `0874a27b17a22e2b7c2e2bcd725768e0803cad06`, path `plugins/backend-development/skills/architecture-patterns` |

## Frontend skills (selected 2026-10-24)

| Skill | One-line purpose | Upstream pin |
| --- | --- | --- |
| react-best-practices | Vercel's 70 prioritised React/Next.js performance rules (waterfalls, bundle size, re-renders), one file per rule. | `vercel-labs/agent-skills` @ `063bee94c3f4df8453406c830b0a7df0f2860278`, path `skills/react-best-practices` |
| composition-patterns | React component API design: compound components, lifted state, explicit variants, React 19 changes. | same repo and commit, path `skills/composition-patterns` |
| emil-design-eng | Emil Kowalski's rules for UI polish and motion: when to animate, easing, durations, springs, reduced motion. | `emilkowalski/skills` @ `2d228630ba1446365f614a108cb8252f1feda202`, path `skills/emil-design-eng` |
| shadcn | Official shadcn/ui skill: composition, forms, semantic tokens, theming, design-system pages. | `shadcn-ui/ui` @ `ffd225775bbb7955a673f08bb9be4a63600d9dfb`, path `skills/shadcn` |
| webapp-testing | Anthropic's Playwright recipe for verifying local frontends (recon-then-act, screenshots, console logs). | `anthropics/skills` @ `dbd4588f9e1033efb41dad4bef2f7947c8993d44`, path `skills/webapp-testing` |
| tailwind-design-system | Tailwind v4 CSS-first tokens, `@theme`, dark mode, variant components, responsive patterns. | `wshobson/agents` @ `0874a27b17a22e2b7c2e2bcd725768e0803cad06`, path `plugins/frontend-mobile-development/skills/tailwind-design-system` |
| accessibility | WCAG 2.2 audit workflow and fixes (contrast, keyboard, focus, ARIA, live regions). | `addyosmani/web-quality-skills` @ `afa8da942115f2961fdbfa80807ea0b232ff6c00`, path `skills/accessibility` |
| performance-optimization | Measure-first Core Web Vitals, bundle, image and re-render workflow with keep-or-revert discipline. | `addyosmani/agent-skills` @ `aeac41d4d005999432bb7312f7ff06383a91d59d`, path `skills/performance-optimization` |
| kpi-dashboard-design | Dashboard hierarchy, KPI selection, chart choice, real-time display patterns (relevant to console dashboards). | `wshobson/agents` @ `0874a27b17a22e2b7c2e2bcd725768e0803cad06`, path `plugins/business-analytics/skills/kpi-dashboard-design` |
| refactoring-ui | Visual hierarchy, spacing, type scales, colour, depth; dark-mode, dataviz and micro-interaction references. | `wondelai/skills` @ `c172996495bed0fcd26896a9416b2093fd7073f0`, path `refactoring-ui` |

## WebGL / Three.js skills (added 2026-10-26)

Source `CloudAI-X/threejs-skills` @ `0e181866f095b5f06d005133876e926515390d95` (MIT), targeting Three.js r186 and R3F 9.8, matching the website's `three@0.186.1` and `@react-three/fiber@9.8.2`.

| Skill | One-line purpose |
| --- | --- |
| threejs-fundamentals | Scene, camera, renderer, transforms, animation loop. |
| threejs-react-three-fiber | React renderer patterns and lifecycle for R3F scenes. |
| threejs-shaders | ShaderMaterial, GLSL and node-material guidance. |
| threejs-performance | Frame cost, draw calls, GPU memory, compile stalls. |

## Rules of use

Selection was by popularity as a signal only, not a security guarantee; the text was reviewed before use. Apply the principles to JARVIS's single-owner local workload: distributed-service recipes are not requirements to add infrastructure.
