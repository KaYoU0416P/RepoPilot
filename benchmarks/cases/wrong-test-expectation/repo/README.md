# 舍入规则

`round_half_up(x)` 采用**四舍五入**（half-up）：

- `0.5` → `1`
- `1.5` → `2`
- `2.5` → `3`
- `-0.5` → `-1`

注意这**不是** Python 内置 `round()` 的行为。内置 `round()` 用的是
banker's rounding（四舍六入五取偶），`round(0.5)` 会得到 `0`。
本项目明确要求 half-up，请勿改成内置 round。
