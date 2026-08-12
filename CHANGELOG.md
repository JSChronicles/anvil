# Changelog

All notable changes to this project will be documented in this file.

<!-- version list -->

## v0.31.0 (2026-08-12)

### Bug Fixes

- **runner**: Optimize graph result assembly ([#68](https://github.com/JSChronicles/anvil/pull/68),
  [`50b471f`](https://github.com/JSChronicles/anvil/commit/50b471ff4f6a561a8c1c6b4f92a3dea36c8714a9))

- **runner**: Preserve dependency order in mixed-scope results
  ([#68](https://github.com/JSChronicles/anvil/pull/68),
  [`50b471f`](https://github.com/JSChronicles/anvil/commit/50b471ff4f6a561a8c1c6b4f92a3dea36c8714a9))

### Chores

- **deps**: Update development tooling ([#68](https://github.com/JSChronicles/anvil/pull/68),
  [`50b471f`](https://github.com/JSChronicles/anvil/commit/50b471ff4f6a561a8c1c6b4f92a3dea36c8714a9))

- **github-actions**: Bump actions/stale in the github-actions group
  ([#66](https://github.com/JSChronicles/anvil/pull/66),
  [`c78e7c9`](https://github.com/JSChronicles/anvil/commit/c78e7c95a7edbebdee212c276aaf66061c436d1a))

- **github-actions**: Bump the github-actions group across 1 directory with 5 updates
  ([#62](https://github.com/JSChronicles/anvil/pull/62),
  [`503d786`](https://github.com/JSChronicles/anvil/commit/503d786c612ceec4ebed0b99c6f26047ea030fab))

- **pre-commit**: Bump https://github.com/astral-sh/uv-pre-commit
  ([#67](https://github.com/JSChronicles/anvil/pull/67),
  [`d58a3ed`](https://github.com/JSChronicles/anvil/commit/d58a3edc27608b4f93b4af13978f972f62c39ad9))

- **uv**: Bump pyasn1 from 0.6.3 to 0.6.4 ([#64](https://github.com/JSChronicles/anvil/pull/64),
  [`9e0718b`](https://github.com/JSChronicles/anvil/commit/9e0718b3d22363c2f66813fe0435f88cc83fc208))

- **uv**: Bump the uv-dependencies group with 14 updates
  ([#65](https://github.com/JSChronicles/anvil/pull/65),
  [`586fcf8`](https://github.com/JSChronicles/anvil/commit/586fcf8033743e450954e13c749354b1f9fef044))

### Documentation

- **anvil-task-builder**: Add payer/management-account-only concurrency reference
  ([#68](https://github.com/JSChronicles/anvil/pull/68),
  [`50b471f`](https://github.com/JSChronicles/anvil/commit/50b471ff4f6a561a8c1c6b4f92a3dea36c8714a9))

- **skills**: Align task-builder guidance with the new task model
  ([#68](https://github.com/JSChronicles/anvil/pull/68),
  [`50b471f`](https://github.com/JSChronicles/anvil/commit/50b471ff4f6a561a8c1c6b4f92a3dea36c8714a9))

- **tasks**: Demonstrate task IDs and result-driven workflows
  ([#68](https://github.com/JSChronicles/anvil/pull/68),
  [`50b471f`](https://github.com/JSChronicles/anvil/commit/50b471ff4f6a561a8c1c6b4f92a3dea36c8714a9))

### Features

- **aws**: Support management account filter keywords
  ([#68](https://github.com/JSChronicles/anvil/pull/68),
  [`50b471f`](https://github.com/JSChronicles/anvil/commit/50b471ff4f6a561a8c1c6b4f92a3dea36c8714a9))

- **tasks**: Add dependency-aware scoped task execution
  ([#68](https://github.com/JSChronicles/anvil/pull/68),
  [`50b471f`](https://github.com/JSChronicles/anvil/commit/50b471ff4f6a561a8c1c6b4f92a3dea36c8714a9))

### Refactoring

- **tasks**: Adopt the provider-neutral task runtime contract
  ([#68](https://github.com/JSChronicles/anvil/pull/68),
  [`50b471f`](https://github.com/JSChronicles/anvil/commit/50b471ff4f6a561a8c1c6b4f92a3dea36c8714a9))

### Testing

- **tasks**: Cover redesigned task execution contracts
  ([#68](https://github.com/JSChronicles/anvil/pull/68),
  [`50b471f`](https://github.com/JSChronicles/anvil/commit/50b471ff4f6a561a8c1c6b4f92a3dea36c8714a9))


## v0.30.1 (2026-07-27)

### Bug Fixes

- Expose entity metadata in result queries
  ([`7d191be`](https://github.com/JSChronicles/anvil/commit/7d191be8e7d1eb1c8132a1c0e2ab4ab0a9923196))

- Format in md files
  ([`7d191be`](https://github.com/JSChronicles/anvil/commit/7d191be8e7d1eb1c8132a1c0e2ab4ab0a9923196))

- Implemented component discovery caching
  ([`7d191be`](https://github.com/JSChronicles/anvil/commit/7d191be8e7d1eb1c8132a1c0e2ab4ab0a9923196))

- Refactor validation, context isolation, and catalog APIs
  ([`7d191be`](https://github.com/JSChronicles/anvil/commit/7d191be8e7d1eb1c8132a1c0e2ab4ab0a9923196))

- Remove ConfigBranch from target pipeline
  ([`7d191be`](https://github.com/JSChronicles/anvil/commit/7d191be8e7d1eb1c8132a1c0e2ab4ab0a9923196))

- Tighten typing for GitHub tasks and tests
  ([`7d191be`](https://github.com/JSChronicles/anvil/commit/7d191be8e7d1eb1c8132a1c0e2ab4ab0a9923196))

### Chores

- **github-actions**: Bump the github-actions group with 6 updates
  ([#60](https://github.com/JSChronicles/anvil/pull/60),
  [`5e41564`](https://github.com/JSChronicles/anvil/commit/5e4156469c60a56bd4db0f42bee5db88b578830d))

- **pre-commit**: Bump the pre-commit group with 2 updates
  ([#59](https://github.com/JSChronicles/anvil/pull/59),
  [`8c230c6`](https://github.com/JSChronicles/anvil/commit/8c230c672d67c30fbd3691d759af54336e6c0d95))


## v0.30.0 (2026-07-22)

### Bug Fixes

- Auth check for github
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))

### Chores

- Format files
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))

- **github-actions**: Bump the github-actions group with 4 updates
  ([#57](https://github.com/JSChronicles/anvil/pull/57),
  [`f84a0c5`](https://github.com/JSChronicles/anvil/commit/f84a0c52838db2e0565e14eaf2a1da07462f81cb))

- **pre-commit**: Bump the pre-commit group with 2 updates
  ([#55](https://github.com/JSChronicles/anvil/pull/55),
  [`dd94d11`](https://github.com/JSChronicles/anvil/commit/dd94d110607da59cd0c5a2b0e3fb6eb299029341))

- **uv**: Bump the uv-dependencies group with 7 updates
  ([#56](https://github.com/JSChronicles/anvil/pull/56),
  [`57536b5`](https://github.com/JSChronicles/anvil/commit/57536b5932a722f9cafc73e83479406b87f6f553))

### Features

- Add Azure/GCP extras and update docs
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))

- Add target-scoped task execution
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))

- Github caching
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))

- GitHub credential chain/profile model
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))

- Require task detail docstrings and revamp examples
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))

- Single-flight GitHub installation client builds
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))

- Unify component discovery and remove legacy loaders
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))

- **cli**: Remove anvil graph command
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))

### Refactoring

- **runner**: Remove legacy AWS execution path
  ([`d935ac7`](https://github.com/JSChronicles/anvil/commit/d935ac730ae9a079582074fca909d1db378f3a48))


## v0.29.2 (2026-06-23)

### Bug Fixes

- Base session
  ([`1b8fd06`](https://github.com/JSChronicles/anvil/commit/1b8fd0688d4eda0f31629f17432a2ae847817918))


## v0.29.1 (2026-06-18)

### Bug Fixes

- Base session
  ([`c29ab22`](https://github.com/JSChronicles/anvil/commit/c29ab224dc06eb9269d3c6bbbc30d7e8d17263b2))


## v0.29.0 (2026-06-17)

### Features

- Add assume_role_in_management for organizations
  ([#52](https://github.com/JSChronicles/anvil/pull/52),
  [`a74c830`](https://github.com/JSChronicles/anvil/commit/a74c8309bc0d33c80c90ec8ec27de4adeffbdc94))


## v0.28.2 (2026-06-11)

### Bug Fixes

- Capture plugin discovery issues and lazy-load callables
  ([#50](https://github.com/JSChronicles/anvil/pull/50),
  [`adbf7ac`](https://github.com/JSChronicles/anvil/commit/adbf7ac8291a6fc38c584a47d1772a8a249710df))

- Expose validation errors and tighten import errors
  ([#50](https://github.com/JSChronicles/anvil/pull/50),
  [`adbf7ac`](https://github.com/JSChronicles/anvil/commit/adbf7ac8291a6fc38c584a47d1772a8a249710df))

### Documentation

- Add caching section and minor README fixes ([#49](https://github.com/JSChronicles/anvil/pull/49),
  [`ec09255`](https://github.com/JSChronicles/anvil/commit/ec092553fa055f9549393a545cc711f9640782ee))


## v0.28.1 (2026-06-09)

### Bug Fixes

- Add post_run processors to example configs ([#48](https://github.com/JSChronicles/anvil/pull/48),
  [`5302d04`](https://github.com/JSChronicles/anvil/commit/5302d04bac6e9d9a6d110c3261d6257c0416f3cb))


## v0.28.0 (2026-06-09)

### Features

- Refactor loader design
  ([`8e916d7`](https://github.com/JSChronicles/anvil/commit/8e916d7aff7903c50193e27cdcc5f43c040bada3))


## v0.27.0 (2026-06-09)

### Features

- Add processor listing to unified list CLI
  ([`a84ebe7`](https://github.com/JSChronicles/anvil/commit/a84ebe71af2f0694ac52f26e18a63bd481f98b96))


## v0.26.0 (2026-06-03)

### Chores

- **github-actions**: Bump the github-actions group with 4 updates
  ([#44](https://github.com/JSChronicles/anvil/pull/44),
  [`0ab837b`](https://github.com/JSChronicles/anvil/commit/0ab837b5ccac2f70ec626f78eea501ca13fa3681))

- **uv**: Bump idna from 3.11 to 3.15 ([#42](https://github.com/JSChronicles/anvil/pull/42),
  [`f289908`](https://github.com/JSChronicles/anvil/commit/f289908800d7dffa92481e9f68320c45b1a426e1))

- **uv**: Bump the uv-dependencies group with 5 updates
  ([#43](https://github.com/JSChronicles/anvil/pull/43),
  [`258157f`](https://github.com/JSChronicles/anvil/commit/258157f9fd15f42c27afef07722dcdb8c0c72cb0))

- **uv**: Bump urllib3 from 2.6.3 to 2.7.0 ([#41](https://github.com/JSChronicles/anvil/pull/41),
  [`f0a9fca`](https://github.com/JSChronicles/anvil/commit/f0a9fca93508554c084df173e4e497774194439b))

### Features

- **tasks**: Add list_lambdas_by_runtime task and config
  ([#45](https://github.com/JSChronicles/anvil/pull/45),
  [`6126f8d`](https://github.com/JSChronicles/anvil/commit/6126f8d35361875c9b0daf020be693f47ea1d3ce))


## v0.25.0 (2026-05-04)

### Features

- Simplify result queries and add smart reruns
  ([#38](https://github.com/JSChronicles/anvil/pull/38),
  [`567ee81`](https://github.com/JSChronicles/anvil/commit/567ee8148e8a8d40cb19cb506989e05e4f3cb76d))


## v0.24.0 (2026-05-03)

### Features

- Add organization region selectors ([#37](https://github.com/JSChronicles/anvil/pull/37),
  [`4587dca`](https://github.com/JSChronicles/anvil/commit/4587dca556a00d2b9f64932f8d829c94cab51712))


## v0.23.2 (2026-05-02)

### Bug Fixes

- **release**: Keep uv.lock synced during releases
  ([`8ec2791`](https://github.com/JSChronicles/anvil/commit/8ec2791a3b8c399b194defef6a0b55997a605eba))


## v0.23.1 (2026-05-02)

### Bug Fixes

- **release**: Remove uv.lock release asset upload
  ([`a18f4ed`](https://github.com/JSChronicles/anvil/commit/a18f4ed20d8c4ff33fbc42d39f8c4f35cfbc58c3))


## v0.23.0 (2026-05-01)

### Chores

- **deps**: Bump the github-actions group with 4 updates
  ([#26](https://github.com/JSChronicles/anvil/pull/26),
  [`b8233f7`](https://github.com/JSChronicles/anvil/commit/b8233f7a9744289580d1609a4ed5f897c17d64d5))

- **deps-dev**: Bump prek in the uv-dependencies group
  ([#25](https://github.com/JSChronicles/anvil/pull/25),
  [`bf178f5`](https://github.com/JSChronicles/anvil/commit/bf178f530b02f745a6025d9d534616030094223e))

- **uv**: Bump prek from 0.3.9 to 0.3.10 in the uv-dependencies group
  ([#33](https://github.com/JSChronicles/anvil/pull/33),
  [`99303de`](https://github.com/JSChronicles/anvil/commit/99303de9faf0b4ab43399051adcaca68fd82f15d))

### Features

- (results) add JSONL result queries ([#34](https://github.com/JSChronicles/anvil/pull/34),
  [`f0f6a42`](https://github.com/JSChronicles/anvil/commit/f0f6a42796a859a4bf1a80acb324a4caa7b26aca))


## v0.22.0 (2026-04-25)

### Features

- Cache optimizations ([#24](https://github.com/JSChronicles/anvil/pull/24),
  [`e1cf92c`](https://github.com/JSChronicles/anvil/commit/e1cf92c85310aa020f32b371ca4637076b26a4f4))


## v0.21.0 (2026-04-24)

### Features

- Engine phase timings ([#22](https://github.com/JSChronicles/anvil/pull/22),
  [`56486c1`](https://github.com/JSChronicles/anvil/commit/56486c1cfb8d05b3c9c1a6bf8d5976a0882a7e60))


## v0.20.0 (2026-04-22)

### Features

- Add bounded parallel region execution ([#20](https://github.com/JSChronicles/anvil/pull/20),
  [`08d955d`](https://github.com/JSChronicles/anvil/commit/08d955ddb04c5890fa6989bafbf1caba93acdde8))


## v0.19.1 (2026-04-11)

### Bug Fixes

- Test release automation ([#18](https://github.com/JSChronicles/anvil/pull/18),
  [`d1097ec`](https://github.com/JSChronicles/anvil/commit/d1097ecb4b8ff706550fd1c1c1ea33026f7f811a))
