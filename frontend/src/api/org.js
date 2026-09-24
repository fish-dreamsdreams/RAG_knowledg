/**
 * 组织架构接口（tasklist 4.1 ~ 4.3，供给 14.7 的系统配置页）。
 *
 * 三个资源三套权限码：部门 `org:dept`、用户 `org:user`、角色 `org:role`。页面按码逐块收起，
 * 持其中任意一个码就能进「组织架构」页签，看不到的部分直接不请求（避免 403 噪音）。
 */

import request from './request'

export function listDepartments() {
  return request.get('/departments')
}

export function createDepartment(payload) {
  return request.post('/departments', payload)
}

export function updateDepartment(departmentId, payload) {
  return request.put(`/departments/${departmentId}`, payload)
}

export function deleteDepartment(departmentId) {
  return request.delete(`/departments/${departmentId}`)
}

export function listUsers(params = {}) {
  return request.get('/users', { params })
}

export function createUser(payload) {
  return request.post('/users', payload)
}

export function updateUser(userId, payload) {
  return request.put(`/users/${userId}`, payload)
}

export function deleteUser(userId) {
  return request.delete(`/users/${userId}`)
}

export function listRoles() {
  return request.get('/roles')
}

export function createRole(payload) {
  return request.post('/roles', payload)
}

export function updateRole(roleId, payload) {
  return request.put(`/roles/${roleId}`, payload)
}

export function deleteRole(roleId) {
  return request.delete(`/roles/${roleId}`)
}

/** 权限码字典（`org:role`）：角色编辑里的勾选项来源。 */
export function listPermissions() {
  return request.get('/permissions')
}
