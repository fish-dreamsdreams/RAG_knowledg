/**
 * 账号相关的校验口径（tasklist 14.7 的用户管理表单）。
 *
 * 与后端 `schemas/org.py` 的 `UserCreate.password` 对齐：口令长度是服务端定的规则，
 * 前端只是提前提示，别在两处写死不同的数字。
 */

/** 口令最小长度（后端 `password: min_length=8`）。 */
export const PASSWORD_MIN_LENGTH = 8
