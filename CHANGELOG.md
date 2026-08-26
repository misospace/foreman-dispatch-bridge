# Changelog

## [0.7.2](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.7.1...v0.7.2) (2026-08-26)


### Bug Fixes

* drop the orphan .rc-check gitlink ([#223](https://github.com/misospace/foreman-dispatch-bridge/issues/223)) ([c62a4b1](https://github.com/misospace/foreman-dispatch-bridge/commit/c62a4b14990e2bf02b92beaf5c450b45d5ef759b))
* send a reason when parking an issue as blocked ([#224](https://github.com/misospace/foreman-dispatch-bridge/issues/224)) ([1b63225](https://github.com/misospace/foreman-dispatch-bridge/commit/1b63225fcfcdd6bcba0b52b0aaeb948706d031a1))


### Chores

* **renovate:** ensure requirements-dev.txt is scanned by Renovate ([#221](https://github.com/misospace/foreman-dispatch-bridge/issues/221)) ([10a56da](https://github.com/misospace/foreman-dispatch-bridge/commit/10a56daf1959853874ca0fc7b5cd02d92975477c)), closes [#55](https://github.com/misospace/foreman-dispatch-bridge/issues/55)

## [0.7.1](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.7.0...v0.7.1) (2026-08-23)


### Bug Fixes

* **bridge:** route run_tick's duplicate closures through BridgeRuntime ([#207](https://github.com/misospace/foreman-dispatch-bridge/issues/207)) ([e05354d](https://github.com/misospace/foreman-dispatch-bridge/commit/e05354d4de84790f8fdd4addc0ac916cb57a910c))
* **claim:** skip only renovate bot issues, log filtered candidates ([#219](https://github.com/misospace/foreman-dispatch-bridge/issues/219)) ([32cd164](https://github.com/misospace/foreman-dispatch-bridge/commit/32cd164dc442294ce1e2ced7b4f7734eb8532396))
* **deps:** pin msgpack exactly in requirements.txt ([#211](https://github.com/misospace/foreman-dispatch-bridge/issues/211)) ([45cf953](https://github.com/misospace/foreman-dispatch-bridge/commit/45cf9537e62040eb4718b1ab160e7c4882d52891)), closes [#172](https://github.com/misospace/foreman-dispatch-bridge/issues/172)
* **env:** sync OPTIONAL_VARS registry with env vars the bridge reads ([#210](https://github.com/misospace/foreman-dispatch-bridge/issues/210)) ([3d9a369](https://github.com/misospace/foreman-dispatch-bridge/commit/3d9a369dff3c3eedc812826069573a5b204a7369)), closes [#173](https://github.com/misospace/foreman-dispatch-bridge/issues/173)
* **release:** synchronize Python project version ([#212](https://github.com/misospace/foreman-dispatch-bridge/issues/212)) ([ac7f596](https://github.com/misospace/foreman-dispatch-bridge/commit/ac7f596bef6779bfd011d1e4b96a3f783b90990f)), closes [#174](https://github.com/misospace/foreman-dispatch-bridge/issues/174)
* **retry:** make human escalation parking idempotent ([#217](https://github.com/misospace/foreman-dispatch-bridge/issues/217)) ([c259c4c](https://github.com/misospace/foreman-dispatch-bridge/commit/c259c4c77fd3e7899d58f268f502e59f6550af0f))
* **retry:** recover infra-parked workloads ([#218](https://github.com/misospace/foreman-dispatch-bridge/issues/218)) ([db7a9f0](https://github.com/misospace/foreman-dispatch-bridge/commit/db7a9f0b8c079151db4a60d86fb7c83e59da2276))
* **review-transition:** report no-PR Workload verdict to dispatch instead of silent skip:no-pr ([#214](https://github.com/misospace/foreman-dispatch-bridge/issues/214)) ([46fcac1](https://github.com/misospace/foreman-dispatch-bridge/commit/46fcac16bc8b3516b67cf4d2d7dadf5a0ae9b939)), closes [#213](https://github.com/misospace/foreman-dispatch-bridge/issues/213)

## [0.7.0](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.6.32...v0.7.0) (2026-08-21)


### Features

* **main:** extract run_tick so a test can drive a whole tick ([#206](https://github.com/misospace/foreman-dispatch-bridge/issues/206)) ([a74e727](https://github.com/misospace/foreman-dispatch-bridge/commit/a74e72788b8bbe50e351ca4cf07ccdccd39dc033))


### Bug Fixes

* **prune:** use a valid annotation key for the terminal-since stamp ([#204](https://github.com/misospace/foreman-dispatch-bridge/issues/204)) ([3abd24d](https://github.com/misospace/foreman-dispatch-bridge/commit/3abd24dcdec71831600ed4b20040b155ff822cbe))

## [0.6.32](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.6.31...v0.6.32) (2026-08-21)


### Bug Fixes

* **prune:** drop the terminal-since stamp when its PATCH fails ([#202](https://github.com/misospace/foreman-dispatch-bridge/issues/202)) ([cd9a0a0](https://github.com/misospace/foreman-dispatch-bridge/commit/cd9a0a037c8f8c77523921939dcf84f07762d0e1))

## [0.6.31](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.6.30...v0.6.31) (2026-08-21)


### Bug Fixes

* **prune:** stop the stamp handler crashing on a reserved log key ([#200](https://github.com/misospace/foreman-dispatch-bridge/issues/200)) ([e6b612a](https://github.com/misospace/foreman-dispatch-bridge/commit/e6b612ace884d73ed4c87ab1c72787c4244a724b))

## [0.6.30](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.6.29...v0.6.30) (2026-08-21)


### Bug Fixes

* **main:** move the entrypoint guard to the end of the module ([#197](https://github.com/misospace/foreman-dispatch-bridge/issues/197)) ([85ba19f](https://github.com/misospace/foreman-dispatch-bridge/commit/85ba19f7ca616a34b08899bef8525202ab15e414))

## [0.6.29](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.6.28...v0.6.29) (2026-08-21)


### Bug Fixes

* **prune:** call the stamping lister from the runtime path ([#194](https://github.com/misospace/foreman-dispatch-bridge/issues/194)) ([4dbcf94](https://github.com/misospace/foreman-dispatch-bridge/commit/4dbcf9416605a13b6cee3f7914ccaea14e7b258f))

## [0.6.28](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.6.27...v0.6.28) (2026-08-21)


### Bug Fixes

* **prune:** stamp terminal-since on the workloads plural ([#192](https://github.com/misospace/foreman-dispatch-bridge/issues/192)) ([7101bca](https://github.com/misospace/foreman-dispatch-bridge/commit/7101bca286d265d0e3c9baf06dfc70a512206f0a))

## [0.6.27](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.6.26...v0.6.27) (2026-08-21)


### Bug Fixes

* 170: prune TTL never elapses ([#190](https://github.com/misospace/foreman-dispatch-bridge/issues/190)) ([f0174c6](https://github.com/misospace/foreman-dispatch-bridge/commit/f0174c621b6ac320c437d70479c60bba6a06e7f6))
* **deps:** update dependency ruff (0.16.3 → 0.16.4) ([#184](https://github.com/misospace/foreman-dispatch-bridge/issues/184)) ([d5bfde0](https://github.com/misospace/foreman-dispatch-bridge/commit/d5bfde05fdd08df6f8c5279939d0f9ad09ba2e8e))
* **prfix:** rebase the PR branch so attempts keep prior commits ([#191](https://github.com/misospace/foreman-dispatch-bridge/issues/191)) ([ec26b19](https://github.com/misospace/foreman-dispatch-bridge/commit/ec26b190add06bf02febe80c0002a512bcc711a6))


### Chores

* untrack the generated egg-info directory ([#188](https://github.com/misospace/foreman-dispatch-bridge/issues/188)) ([fe30afa](https://github.com/misospace/foreman-dispatch-bridge/commit/fe30afa4cf16705d6f749dd7cc50998172fb7f68))

## [0.6.26](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.6.25...v0.6.26) (2026-08-20)


### Bug Fixes

* **slots:** free coder during review ([#182](https://github.com/misospace/foreman-dispatch-bridge/issues/182)) ([02eb30f](https://github.com/misospace/foreman-dispatch-bridge/commit/02eb30fbbdc7d1753a620c475a016e7a2360f010))
