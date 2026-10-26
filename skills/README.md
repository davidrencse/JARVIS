# Project-local reference skills

The two architecture skills were downloaded and reviewed on 2026-10-21; the ten frontend skills on 2026-10-24. These are reference skills, not Python runtime dependencies. Their contents and upstream license notices (MIT, or Apache-2.0 for `webapp-testing`) are preserved. No downloaded executable scripts were run, and upstream executable scripts were omitted (see below).

## Architecture skills

| Skill | Why selected | Pinned upstream source |
| --- | --- | --- |
| [System design](system-design/SKILL.md) | Requirements, capacity estimates, reliability and architecture tradeoffs. Registry showed approximately 6,000 installs; repository had 2,478 stars at inspection. | [wondelai/skills, c172996](https://github.com/wondelai/skills/tree/c172996495bed0fcd26896a9416b2093fd7073f0/system-design) |
| [Architecture patterns](architecture-patterns/SKILL.md) | Module boundaries, dependency direction and testable adapters, including Python examples. Registry showed approximately 24,000 installs; repository had 41,426 stars at inspection. | [wshobson/agents, 0874a27](https://github.com/wshobson/agents/tree/0874a27b17a22e2b7c2e2bcd725768e0803cad06/plugins/backend-development/skills/architecture-patterns) |

## Frontend skills

Popularity figures are skills.sh install counts and GitHub repository stars observed on 2026-10-24.

| Skill | Why selected | Pinned upstream source |
| --- | --- | --- |
| [React best practices](react-best-practices/SKILL.md) | Vercel Engineering's 70 prioritized React/Next.js performance rules (waterfalls, bundle size, re-renders), one file per rule. Registry showed approximately 835,000 installs; repository had 32,944 stars. | [vercel-labs/agent-skills, 063bee9](https://github.com/vercel-labs/agent-skills/tree/063bee94c3f4df8453406c830b0a7df0f2860278/skills/react-best-practices) |
| [Composition patterns](composition-patterns/SKILL.md) | React component API design: compound components, lifted state, explicit variants instead of boolean props, React 19 changes. Registry showed approximately 415,000 installs; same repository. | [vercel-labs/agent-skills, 063bee9](https://github.com/vercel-labs/agent-skills/tree/063bee94c3f4df8453406c830b0a7df0f2860278/skills/composition-patterns) |
| [Design engineering](emil-design-eng/SKILL.md) | Emil Kowalski's (Sonner, Vaul) rules for UI polish and motion: when to animate, easing, durations, springs, reduced motion. Registry showed approximately 384,000 installs; repository had 49,298 stars. | [emilkowalski/skills, 2d22863](https://github.com/emilkowalski/skills/tree/2d228630ba1446365f614a108cb8252f1feda202/skills/emil-design-eng) |
| [shadcn/ui](shadcn/SKILL.md) | Official shadcn/ui skill: component composition, forms, semantic tokens, theming and design-system pages. Registry showed approximately 271,000 installs; repository had 126,428 stars. | [shadcn/ui, ffd2257](https://github.com/shadcn-ui/ui/tree/ffd225775bbb7955a673f08bb9be4a63600d9dfb/skills/shadcn) |
| [Web app testing](webapp-testing/SKILL.md) | Anthropic's Playwright recipe for verifying local frontends: reconnaissance-then-action, screenshots, console logs. Registry showed approximately 187,000 installs; repository had 183,459 stars. | [anthropics/skills, dbd4588](https://github.com/anthropics/skills/tree/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills/webapp-testing) |
| [Tailwind design system](tailwind-design-system/SKILL.md) | Tailwind CSS v4 CSS-first tokens, `@theme`, dark mode, variant components and responsive patterns. Registry showed approximately 70,000 installs; repository had 41,628 stars. | [wshobson/agents, 0874a27](https://github.com/wshobson/agents/tree/0874a27b17a22e2b7c2e2bcd725768e0803cad06/plugins/frontend-mobile-development/skills/tailwind-design-system) |
| [Accessibility](accessibility/SKILL.md) | WCAG 2.2 audit workflow and fixes (contrast, keyboard, focus, ARIA, live regions), with WCAG and pattern references. Registry showed approximately 64,000 installs; repository had 2,984 stars (author Addy Osmani). | [addyosmani/web-quality-skills, afa8da9](https://github.com/addyosmani/web-quality-skills/tree/afa8da942115f2961fdbfa80807ea0b232ff6c00/skills/accessibility) |
| [Performance optimization](performance-optimization/SKILL.md) | Measure-first workflow for Core Web Vitals, bundles, images and re-renders, with keep-or-revert discipline. Registry showed approximately 64,000 installs; repository had 110,144 stars. | [addyosmani/agent-skills, aeac41d](https://github.com/addyosmani/agent-skills/tree/aeac41d4d005999432bb7312f7ff06383a91d59d/skills/performance-optimization) |
| [KPI dashboard design](kpi-dashboard-design/SKILL.md) | Dashboard hierarchy, KPI selection, chart choice and real-time display patterns; relevant to the console dashboards. Registry showed approximately 16,000 installs; repository had 41,628 stars. | [wshobson/agents, 0874a27](https://github.com/wshobson/agents/tree/0874a27b17a22e2b7c2e2bcd725768e0803cad06/plugins/business-analytics/skills/kpi-dashboard-design) |
| [Refactoring UI](refactoring-ui/SKILL.md) | Visual hierarchy, spacing and type scales, color, depth, plus references on dark mode, data visualization and micro-interactions. Registry showed approximately 9,000 installs; repository had 2,503 stars. | [wondelai/skills, c172996](https://github.com/wondelai/skills/tree/c172996495bed0fcd26896a9416b2093fd7073f0/refactoring-ui) |

Notes on the frontend copies:

- `react-best-practices` and `composition-patterns` declare `license: MIT` in their `SKILL.md` frontmatter, but the upstream repository has no LICENSE file at the pinned commit, so none is included here.
- `webapp-testing` keeps upstream `LICENSE.txt` (Apache-2.0). Its `scripts/with_server.py` and `examples/*.py` were omitted because they are executable; the SKILL.md still refers to them.
- `shadcn` omits `scripts/showcase-coverage.mjs` (executable). Its SKILL.md contains a ``!`npx shadcn@latest info --json` `` context-injection line that would run the shadcn CLI if the skill were registered with an agent that supports it; as a local reference it does not run.
- `performance-optimization` links to `../../references/performance-checklist.md`, a repository-level upstream file that is not part of this copy.
- `frontend-design` was not duplicated; it is already installed globally.

Popularity was a selection signal, not a security guarantee or proof these are universally the best skills. The actual skill text was reviewed before use. Their general principles were applied to JARVIS's single-owner local workload; distributed-service recipes are not requirements to add infrastructure.

## WebGL / Three.js skills

Four project-local references were added on 2026-10-26 from [CloudAI-X/threejs-skills](https://github.com/CloudAI-X/threejs-skills), pinned to commit `0e181866f095b5f06d005133876e926515390d95` (MIT). The upstream guidance targets Three.js r186 and React Three Fiber 9.8, matching this project's `three@0.186.1` and `@react-three/fiber@9.8.2` dependencies.

| Skill | Why selected |
| --- | --- |
| [Three.js fundamentals](threejs-fundamentals/SKILL.md) | Scene, camera, renderer, transforms, and animation loop for the site's 3D scenes. |
| [React Three Fiber](threejs-react-three-fiber/SKILL.md) | React renderer patterns and lifecycle guidance for the app's R3F scenes. |
| [Shaders](threejs-shaders/SKILL.md) | ShaderMaterial, GLSL, and node material guidance for the custom visuals. |
| [Performance](threejs-performance/SKILL.md) | Frame cost, draw calls, GPU memory, and compile stalls for the WebGL sections. |

`sources.json` records the source revision and SHA-256 checksums. These are project-local references, not automatically registered agent skills.

Registries: [system-design](https://www.skills.sh/wondelai/skills/system-design), [architecture-patterns](https://www.skills.sh/wshobson/agents/architecture-patterns), [vercel-react-best-practices](https://www.skills.sh/vercel-labs/agent-skills/vercel-react-best-practices), [vercel-composition-patterns](https://www.skills.sh/vercel-labs/agent-skills/vercel-composition-patterns), [emil-design-eng](https://www.skills.sh/emilkowalski/skills/emil-design-eng), [shadcn](https://www.skills.sh/shadcn/ui/shadcn), [webapp-testing](https://www.skills.sh/anthropics/skills/webapp-testing), [tailwind-design-system](https://www.skills.sh/wshobson/agents/tailwind-design-system), [accessibility](https://www.skills.sh/addyosmani/web-quality-skills/accessibility), [performance-optimization](https://www.skills.sh/addyosmani/agent-skills/performance-optimization), [kpi-dashboard-design](https://www.skills.sh/wshobson/agents/kpi-dashboard-design), [refactoring-ui](https://www.skills.sh/wondelai/skills/refactoring-ui).

## Availability

All twelve skills and their local references (minus the omitted scripts noted above) are downloaded under this writable project folder. The session's global and project agent skill directories are read-only. Consequently this is **not a global installation or automatic Codex skill registration**. They remain available as local references on subsequent turns; ask the agent to read the linked `SKILL.md`.

The standard Skills CLI and installer could not reach their download endpoints from this environment. The downloads used the permitted GitHub CLI, pinned revisions, and the Contents API. See `sources.json` for source revisions and file SHA-256 checksums.

To register globally later from a regular terminal:

```powershell
npx skills add wondelai/skills@system-design -g -y
npx skills add wshobson/agents@architecture-patterns -g -y
npx skills add vercel-labs/agent-skills@vercel-react-best-practices -g -y
npx skills add vercel-labs/agent-skills@vercel-composition-patterns -g -y
npx skills add emilkowalski/skills@emil-design-eng -g -y
npx skills add shadcn/ui@shadcn -g -y
npx skills add anthropics/skills@webapp-testing -g -y
npx skills add wshobson/agents@tailwind-design-system -g -y
npx skills add addyosmani/web-quality-skills@accessibility -g -y
npx skills add addyosmani/agent-skills@performance-optimization -g -y
npx skills add wshobson/agents@kpi-dashboard-design -g -y
npx skills add wondelai/skills@refactoring-ui -g -y
```

Those commands install the upstream version available at that time; the copies here remain pinned. Globally registered skills become available to Codex on a subsequent turn.
