export function composeGmvTaskQueryKey(workspaceId, provider, authId, taskId) {
  const normalizedId = taskId ? String(taskId).trim() : "";
  return ["gmvmax-task", workspaceId, provider, authId, normalizedId || undefined];
}

export default composeGmvTaskQueryKey;
