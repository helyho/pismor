<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => String(route.meta.title ?? '功能'))
const note = computed(() => String(route.meta.note ?? '该功能尚未接入。'))
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>{{ title }}</h2>
    </div>
    <el-card shadow="never">
      <el-empty :description="note" />
    </el-card>
  </div>
</template>
