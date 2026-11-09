# Contributing to Brain OS

Thank you for your interest in contributing to Brain OS!

## 1. Development & Mirroring Model

Brain OS uses a **monorepo-first development model**.
- Public and client-facing repositories are generated release mirrors.
- Pull Requests opened in mirror repositories are reviewed and absorbed into the primary monorepo with full author attribution preserved (`git am`).
- Once absorbed, your contribution appears in the next published release tag.

## 2. Developer Certificate of Origin (DCO)

To ensure clear licensing and provenance, all contributions must include a DCO sign-off in commit messages:

```bash
git commit -s -m "feat(module): add meaningful description"
```

By adding a `Signed-off-by:` line, you certify that:
1. The contribution was created in whole or in part by you, and you have the right to submit it under the AGPL-3.0 license.
2. The contribution is based upon previous work that, to the best of your knowledge, is covered under an appropriate open-source license.
3. The contribution is provided directly to the project under the GNU Affero General Public License v3.0.

## 3. Scope of Contributions

- Keep PRs focused on specific components (`apps/<app>` or standalone tools).
- Changes affecting global architecture, shared contracts (`BRAIN_*`), or deployment templates should first be discussed in an issue.
- All contributions must pass CI and security linting before being accepted.

## 4. Code of Conduct & Ethics

Tools such as `key-logger` and telemetry collectors are designed strictly for personal device observability and self-hosting telemetry on hardware you own or have explicit authorization to monitor. Any contribution designed to bypass consent or exfiltrate private data to unauthorized third parties will be rejected.
