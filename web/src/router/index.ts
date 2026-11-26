import { createRouter, createWebHistory } from 'vue-router'

import AdminLayout from '@/layouts/AdminLayout.vue'
import { useAuthStore } from '@/auth'

const router = createRouter({
  history: createWebHistory('/admin/'),
  routes: [
    {
      path: '/',
      component: AdminLayout,
      children: [
        {
          path: '',
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
          meta: { title: '概览', capability: 'system.read' },
        },
        {
          path: 'review',
          name: 'review',
          component: () => import('@/views/ReviewView.vue'),
          meta: { title: '复核', capability: 'review.read' },
        },
        {
          path: 'users',
          name: 'users',
          component: () => import('@/views/UsersView.vue'),
          meta: { title: '用户', capability: 'profile.read' },
        },
        {
          path: 'users/:source/:user',
          name: 'user-detail',
          component: () => import('@/views/UserDetailView.vue'),
          meta: { title: '用户记忆', capability: 'profile.read' },
        },
        {
          path: 'entries',
          name: 'entries',
          component: () => import('@/views/EntriesView.vue'),
          meta: { title: '记忆', capability: 'profile.read' },
        },
        {
          path: 'system',
          name: 'system',
          component: () => import('@/views/SystemView.vue'),
          meta: { title: '系统', capability: 'system.read' },
        },
        {
          path: 'config',
          name: 'config',
          component: () => import('@/views/ConfigView.vue'),
          meta: { title: '配置', capability: 'settings.read' },
        },
      ],
    },
    {
      path: '/forbidden',
      name: 'forbidden',
      component: () => import('@/views/ForbiddenView.vue'),
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFoundView.vue'),
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.loaded) {
    try {
      await auth.load()
    } catch {
      // 认证接口不可用时仍让页面渲染错误态，避免路由死循环。
    }
  }
  const capability = to.meta.capability as string | undefined
  if (capability && !auth.can(capability)) {
    return { name: 'forbidden', query: { from: to.fullPath } }
  }
})

export default router
