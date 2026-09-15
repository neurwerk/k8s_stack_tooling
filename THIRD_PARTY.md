# Maintenance Theme Assets

The maintenance responder uses a versioned subset of the local
`keycloak_theme` repository at commit `32f99f2833ee4e2acb528bbe2c0702900d24beed`
(theme source version 0.1.2). Its shared palette and layout are adapted from
`theme/neurwerk/login/resources/css/neurwerk.css`. No Keycloak templates,
scripts, client logo, favicon, or authentication dependencies are included.

`src/k8s_stack_tooling/maintenance/sync_assets.py` records the source paths and
SHA-256 checksums and reproduces the bundled binary subset without downloads.
It reads only the explicit local `keycloak_theme` checkout.

- `Inter-Regular.ttf` and `Inter-SemiBold.ttf`: Copyright The Inter Project
  Authors; SIL Open Font License 1.1. The complete license is bundled at
  `src/k8s_stack_tooling/maintenance/assets/OFL.txt` and in the installed package.
  Upstream license: https://github.com/rsms/inter/blob/v4.1/LICENSE.txt.
- `logo_black.png`: Neurwerk wordmark, originally retrieved by the theme project
  from https://www.neurwerk.com/assets/images/logo_black.png on 2026-09-09.
  It remains a Neurwerk brand asset; the code license grants no trademark rights.
- Font source URLs recorded by the theme project:
  https://www.neurwerk.com/assets/fonts/Inter-Regular.ttf and
  https://www.neurwerk.com/assets/fonts/Inter-SemiBold.ttf.

All assets are local at runtime. Client branding is operator-supplied, not
bundled in this package.
