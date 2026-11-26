<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import {
  ChatDotRound,
  Collection,
  Odometer,
  Setting,
  Tools,
  User,
} from '@element-plus/icons-vue'

import { useAuthStore } from '@/auth'

const auth = useAuthStore()
const route = useRoute()

const navGroups = [
  {
    label: '工作台',
    items: [
      { path: '/', label: '概览', icon: Odometer, capability: 'system.read' },
      { path: '/review', label: '复核', icon: ChatDotRound, capability: 'review.read' },
    ],
  },
  {
    label: '数据',
    items: [
      { path: '/users', label: '用户', icon: User, capability: 'profile.read' },
      { path: '/entries', label: '记忆', icon: Collection, capability: 'profile.read' },
    ],
  },
  {
    label: '系统',
    items: [
      { path: '/config', label: '配置', icon: Setting, capability: 'settings.read' },
      { path: '/system', label: '系统', icon: Tools, capability: 'system.read' },
    ],
  },
]

const pageDescriptions: Record<string, string> = {
  '/': '运行状态、记忆规模与待处理事项',
  '/review': '集中处理待复核记忆，支持键盘连续操作',
  '/users': '按来源与用户 ID 查找记忆',
  '/entries': '跨用户检索、筛选与编辑记忆',
  '/system': '健康检查、日批记录与后台操作审计',
  '/config': '运行参数与维度词表配置',
}

const visibleGroups = computed(() =>
  navGroups
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => auth.can(item.capability)),
    }))
    .filter((group) => group.items.length > 0),
)

const pageTitle = computed(() => String(route.meta.title ?? '管理后台'))
const activeMenu = computed(() => (route.path.startsWith('/users/') ? '/users' : route.path))
const pageDescription = computed(() => {
  if (route.name === 'user-detail') return '查看指定用户的记忆与装配预览'
  return pageDescriptions[route.path] ?? 'pismor 管理后台'
})
const initial = computed(() => (auth.displayName || 'A').slice(0, 1).toUpperCase())

onMounted(() => {
  if (!auth.loaded) auth.load().catch(() => {})
})
</script>

<template>
  <el-container class="admin-shell">
    <el-aside width="232px" class="admin-aside">
      <div class="brand">
        <span class="brand-mark">P</span>
        <span class="brand-name">pismor</span>
        <span class="brand-sub">admin</span>
      </div>
      <el-menu :default-active="activeMenu" router class="admin-menu">
        <el-menu-item-group v-for="group in visibleGroups" :key="group.label">
          <template #title>{{ group.label }}</template>
          <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
            <el-icon><component :is="item.icon" /></el-icon>
            <span>{{ item.label }}</span>
          </el-menu-item>
        </el-menu-item-group>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="admin-header">
        <div class="header-left">
          <div class="header-breadcrumb">
            <span>pismor</span>
            <span class="sep">/</span>
            <strong>{{ pageTitle }}</strong>
          </div>
          <span class="header-subtitle">{{ pageDescription }}</span>
        </div>
        <div class="header-user">
          <span class="avatar-dot">{{ initial }}</span>
          <el-tag size="small" effect="plain" round>{{ auth.displayName }}</el-tag>
        </div>
      </el-header>
      <el-main class="admin-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
