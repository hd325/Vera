// Installed beside Tencent's pinned authorization module as an independent
// fail-closed boundary. It does not import or depend on the custom plugin.
export function createInboundAuthorizer({fetchImpl=fetch,baseUrl=process.env.HOUKONG_WECHAT_BACKEND_URL||'http://127.0.0.1:8000',token=process.env.HOUKONG_WECHAT_SERVICE_TOKEN}={}) {
  return async ({accountId,senderId})=>{
    if(!token||typeof accountId!=='string'||!accountId||typeof senderId!=='string'||!senderId)return false;
    try{
      const url=new URL(baseUrl);
      if(url.protocol!=='http:'||!['127.0.0.1','localhost','[::1]'].includes(url.hostname)||url.username||url.password)return false;
      const response=await fetchImpl(url.origin+'/internal/wechat/authorize-inbound',{method:'POST',headers:{'Content-Type':'application/json','X-Houkong-Wechat-Token':token},body:JSON.stringify({account_id:accountId,sender_id:senderId}),signal:AbortSignal.timeout(3500),redirect:'error'});
      if(!response.ok)return false;
      const result=await response.json();
      return (result.data??result).authorized===true;
    }catch{return false;}
  };
}
export const authorizeHouKongInbound=params=>createInboundAuthorizer()(params);
