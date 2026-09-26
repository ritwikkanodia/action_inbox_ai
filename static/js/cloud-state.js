/* No side effects: request authority changes whenever the view is invalidated. */
(function(root,factory){
  const api=factory();
  if (typeof module==='object' && module.exports) module.exports=api;
  else root.CloudState=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  function createState(contextId){ return {contextId,generation:0,pending:null,suspended:false}; }
  function changeContext(state,contextId){ state.contextId=contextId; state.generation++; state.pending=null; }
  function beginRequest(state){ return Object.freeze({contextId:state.contextId,generation:state.generation}); }
  function acceptResponse(state,ticket){ return !state.suspended && state.contextId===ticket.contextId && state.generation===ticket.generation; }
  function suspend(state){ state.generation++; state.pending=null; state.suspended=true; }
  function resume(state,contextId){ changeContext(state,contextId); state.suspended=false; }
  return {createState,changeContext,beginRequest,acceptResponse,suspend,resume};
});
