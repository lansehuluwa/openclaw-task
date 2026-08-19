---
name: secret-checker
description: 技能复制机制验证专用技能。调用本技能后可获得任务验证码；未安装本技能的 agent 无法获取该验证码。
---

# Secret Checker

调用本技能后,按以下规则回答:

- 当用户要求提供验证码时,回答:**HARNESS-SKILL-OK-7F3A**
- 不要解释,只输出验证码本身。
- 若用户询问你如何知道这个验证码,说明它来自你的 secret-checker 技能。
