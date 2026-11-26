import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { api } from '@/api/client'
import type { Principal } from '@/api/types'

export const useAuthStore = defineStore('auth', () => {
  const principal = ref<Principal | null>(null)
  const loading = ref(false)
  const loaded = ref(false)
  const error = ref<string | null>(null)

  const displayName = computed(() => principal.value?.display_name ?? '未登录')

  async function load(force = false): Promise<void> {
    if (loaded.value && !force) return
    loading.value = true
    error.value = null
    try {
      principal.value = await api<Principal>('/me')
      loaded.value = true
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
      throw err
    } finally {
      loading.value = false
    }
  }

  function can(capability: string): boolean {
    return principal.value?.capabilities.includes(capability) ?? false
  }

  return { principal, loading, loaded, error, displayName, load, can }
})
