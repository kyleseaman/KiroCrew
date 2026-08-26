/** Runnable deployment commands shown for copy/paste in the Coder settings docs. */
export const WORKSPACE_COMMAND =
  'coder templates push kirocrew-arm --yes --directory deploy/coder-aws/workspace'
export const CONTROL_PLANE_COMMAND = 'terraform -chdir=deploy/coder-aws/control-plane apply'
