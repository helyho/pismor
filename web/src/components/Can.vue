<script setup lang="ts">
import { computed } from 'vue'

import { useAuthStore } from '@/auth'

const props = defineProps<{ capability: string }>()

const auth = useAuthStore()
const allowed = computed(() => auth.can(props.capability))
</script>

<template>
  <slot v-if="allowed" />
  <slot v-else name="fallback" />
</template>
