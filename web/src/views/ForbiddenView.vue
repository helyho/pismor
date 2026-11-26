<template>
  <div class="page centered">
    <el-result icon="warning" title="403" sub-title="当前账号没有访问该页面的权限。">
      <template #extra>
        <router-link to="/"><el-button type="primary">返回概览</el-button></router-link>
      </template>
    </el-result>
  </div>
</template>
