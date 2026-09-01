# Changelog

## [0.7.6](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.7.5...v0.7.6) (2026-09-01)


### Bug Fixes

* **retry:** stop scraping "name" as the failed model ([#250](https://github.com/misospace/foreman-dispatch-bridge/issues/250)) ([2a5d009](https://github.com/misospace/foreman-dispatch-bridge/commit/2a5d009867fe458d298caeae001dda688e8de84d))

## [0.7.5](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.7.4...v0.7.5) (2026-08-31)


### Bug Fixes

* **prfix:** an unknown merge state is not a mergeable PR ([#247](https://github.com/misospace/foreman-dispatch-bridge/issues/247)) ([5a3aba2](https://github.com/misospace/foreman-dispatch-bridge/commit/5a3aba2c4aa910cfb2e54107385d0b4458c19d99))


### Chores

* **bridge:** deduplicate _TOKEN_RE/_redact_token into http_retry ([#243](https://github.com/misospace/foreman-dispatch-bridge/issues/243)) ([a3cdffa](https://github.com/misospace/foreman-dispatch-bridge/commit/a3cdffa0b1b880652da265332683c9bd743d3dbe)), closes [#177](https://github.com/misospace/foreman-dispatch-bridge/issues/177)
* **container:** update image docker.io/library/python (ce40764 → cae66f2) ([#246](https://github.com/misospace/foreman-dispatch-bridge/issues/246)) ([df7d9de](https://github.com/misospace/foreman-dispatch-bridge/commit/df7d9de789513a34ccc10b80bfe49c2ef63c3fcd))

## [0.7.4](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.7.3...v0.7.4) (2026-08-27)


### Bug Fixes

* **deps:** update dependency msgpack (1.2.1 → 1.2.2) ([#240](https://github.com/misospace/foreman-dispatch-bridge/issues/240)) ([0a27f6b](https://github.com/misospace/foreman-dispatch-bridge/commit/0a27f6b308e62e1fe5e460186c36e4053b0e514a))
* **deps:** update dependency ruff (0.16.4 → 0.16.5) ([#241](https://github.com/misospace/foreman-dispatch-bridge/issues/241)) ([28b38ff](https://github.com/misospace/foreman-dispatch-bridge/commit/28b38ff1c9d6be67f10ee08c1343752b6c647fad))
* **prfix:** treat CHANGES_REQUESTED as actionable, not as a blocker ([#242](https://github.com/misospace/foreman-dispatch-bridge/issues/242)) ([57becd2](https://github.com/misospace/foreman-dispatch-bridge/commit/57becd293f2b5f323adf2afeb55dbfa253d89727))
* **release:** make the release job green when the release succeeded ([#238](https://github.com/misospace/foreman-dispatch-bridge/issues/238)) ([13598e9](https://github.com/misospace/foreman-dispatch-bridge/commit/13598e96a7b5baf9b42dc25a5995a2b690ffe779))

## [0.7.3](https://github.com/misospace/foreman-dispatch-bridge/compare/v0.7.2...v0.7.3) (2026-08-26)


### Bug Fixes

* **prfix:** keep tombstone on a failed FIXED mark instead of re-running the coder ([#231](https://github.com/misospace/foreman-dispatch-bridge/issues/231)) ([40e7c78](https://github.com/misospace/foreman-dispatch-bridge/commit/40e7c7804e8fc04d7bb5ed498542c574c5294fca)), closes [#228](https://github.com/misospace/foreman-dispatch-bridge/issues/228)
* **prune:** retire parked-Failed tombstones without resetting the issue ([#232](https://github.com/misospace/foreman-dispatch-bridge/issues/232)) ([9a1e9a3](https://github.com/misospace/foreman-dispatch-bridge/commit/9a1e9a3d66a903f71fdff203b85e80d0a9e2e2b5)), closes [#227](https://github.com/misospace/foreman-dispatch-bridge/issues/227)
* **release:** patch openssl for CVE-2026-14456 until the base rebuilds ([#235](https://github.com/misospace/foreman-dispatch-bridge/issues/235)) ([c71141a](https://github.com/misospace/foreman-dispatch-bridge/commit/c71141a3bfbd9e4ea47ed1c8d4e807da4b250479))
* **retry:** bound infra retries with their own counter ([#230](https://github.com/misospace/foreman-dispatch-bridge/issues/230)) ([46d97f5](https://github.com/misospace/foreman-dispatch-bridge/commit/46d97f5ca1dc2d60c95a5399b68cc820140f6234)), closes [#225](https://github.com/misospace/foreman-dispatch-bridge/issues/225)
* **retry:** isolate wedged deletes in give-up branches and dedupe escalation comments ([#233](https://github.com/misospace/foreman-dispatch-bridge/issues/233)) ([dbd1783](https://github.com/misospace/foreman-dispatch-bridge/commit/dbd178345b80bc64e13e4c36dbc1e45e3f8e0b4e)), closes [#226](https://github.com/misospace/foreman-dispatch-bridge/issues/226)
* **review-transition:** park GO-with-no-PR for a human ([#234](https://github.com/misospace/foreman-dispatch-bridge/issues/234)) ([cc50fa1](https://github.com/misospace/foreman-dispatch-bridge/commit/cc50fa191238b5d497c3c3b0f362ee80a2ca67b3)), closes [#229](https://github.com/misospace/foreman-dispatch-bridge/issues/229)
* **tests:** point the give-up fixtures at the infra counter ([#237](https://github.com/misospace/foreman-dispatch-bridge/issues/237)) ([d85efce](https://github.com/misospace/foreman-dispatch-bridge/commit/d85efceee68731315aba7cb4966a1d7179c156d7))

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
