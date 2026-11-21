export function sanitizeAccountHealth(accountIds,snapshots,isPaused,now=Date.now()) {
  const byId=new Map((snapshots??[]).filter(x=>typeof x?.accountId==='string').map(x=>[x.accountId,x]));
  return accountIds.map(account_id=>{
    const snapshot=byId.get(account_id);
    const knownAuthFailure=snapshot?.loggedOut===true || /(?:errcode\s*[:=]?\s*-14|(?:http|status)\s*[:=]?\s*401)\b/i.test(snapshot?.lastError??'');
    const auth_expired=Boolean(isPaused(account_id)||knownAuthFailure||snapshot?.state==='expired');
    let online=null;
    if(auth_expired||snapshot?.state==='expired'||snapshot?.state==='offline')online=false;
    else if(snapshot?.state==='online')online=true;
    else if(snapshot?.state==='unknown'||snapshot?.state==='disconnected'||snapshot?.running===false)online=null;
    else if(snapshot?.connected===false)online=false;
    else if(snapshot?.running===true && typeof (snapshot.lastPollAt??snapshot.lastEventAt)==='number'){
      const recent=now-(snapshot.lastPollAt??snapshot.lastEventAt)<120000;
      online=recent&&snapshot.connected!==false?true:null;
    }
    else if(snapshot?.connected===true)online=true;
    return {account_id,auth_expired,online};
  });
}
