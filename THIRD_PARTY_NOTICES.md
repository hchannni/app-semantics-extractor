# Third-party applications

This repository does not vendor the three evaluated Android applications. Each fetch script
clones a pinned upstream revision, whose source remains governed by its upstream license.

| Application | Upstream repository | Pinned revision | Upstream license |
| --- | --- | --- | --- |
| SimpleAlarmClock | <https://github.com/yuriykulikov/AlarmClock> | `627c7e8731cc13b78d36fdf37efd7dc46c81589c` | Apache-2.0 |
| Fossify Calendar | <https://github.com/FossifyOrg/Calendar> | `59cf4ecc2516b966561e57521594844dbe5cecef` | GPL-3.0 |
| Markor | <https://github.com/gsantner/markor> | `a44733a61cce0faf53df7e8dd2f7c0428dc2b2b5` | Apache-2.0 |

The `app_manifest.json` file under each sample is the machine-readable source of this mapping.
No license is granted for the research code in this snapshot until the repository owner adds
an explicit top-level license.
