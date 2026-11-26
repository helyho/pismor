<template>
  <div class="page centered">
    <el-result icon="info" title="404" sub-title="页面不存在。">
      <template #extra>
        <router-link to="/"><el-button type="primary">返回概览</el-button></router-link>
      </template>
    </el-result>
  </div>
</template>
