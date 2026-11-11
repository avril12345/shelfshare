const $=s=>document.querySelector(s), esc=s=>(s??'').toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,opts={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},credentials:'same-origin',...opts,body:opts.body?JSON.stringify(opts.body):undefined});
  const j=await r.json().catch(()=>({}));if(!r.ok)throw Object.assign(new Error(j.error||r.statusText),{data:j});return j}

async function boot(){const m=await api('/api/me');
  if(m.user){$('#auth').classList.add('hidden');$('#app').classList.remove('hidden');
    $('#who').innerHTML=`<span class="dim small">${esc(m.user)} · ${m.book_count} volumes</span><br><button class="ghost" id="out">Leave library</button>`;
    $('#out').onclick=async()=>{await api('/api/logout',{method:'POST'});location.reload()};loadLocs();if($('#q').value.trim().length<2&&!$('#tab-search').classList.contains('hidden'))loadSuggestions();
    const ss=$('#shelfSel'),pv=ss.value;ss.innerHTML='<option value="">'+esc(m.shelf_name||'My shelf')+'</option>'+(m.shelves||[]).map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');if(pv)ss.value=pv;ss.classList.toggle('hidden',!(m.shelves||[]).length)}
  else{$('#auth').classList.remove('hidden');$('#app').classList.add('hidden');$('#who').innerHTML=''}}
async function auth(kind){try{await api('/api/'+kind,{method:'POST',body:{username:$('#u').value,password:$('#p').value,invite:$('#inv').value}});boot()}catch(e){$('#authMsg').textContent=e.message}}
$('#loginBtn').onclick=()=>auth('login');$('#signupBtn').onclick=()=>auth('signup');
$('#p').onkeydown=e=>{if(e.key==='Enter')auth('login')};

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('hidden',t.id!=='tab-'+b.dataset.tab));
  if(b.dataset.tab==='mine')loadMine();if(b.dataset.tab==='search')doSearch();if(b.dataset.tab==='reqs')loadReqs();if(b.dataset.tab==='invites')loadInv();if(b.dataset.tab==='groups')loadGroups();if(b.dataset.tab==='friends')loadFriends();if(b.dataset.tab==='clubs')showClubList();else stopChat();if(b.dataset.tab!=='add')stopScan()});

// ---- search ----
let st;$('#q').oninput=()=>{clearTimeout(st);st=setTimeout(doSearch,250)};
async function doSearch(){const q=$('#q').value.trim(),el=$('#results');if(q.length<2){loadSuggestions();return}
  const {results}=await api('/api/search?q='+encodeURIComponent(q));
  if(!results.length){el.innerHTML=`<div class="none">No card in the catalogue. Nobody has this yet.</div>`;return}
  const mineAny=results.some(r=>r.mine.length);
  el.innerHTML=`<div class="verdict">${mineAny?'Yes, it is on your shelves.':'Not on your shelves'+(results.some(r=>r.friends.length)?', but another member holds a copy.':'.')}</div>`+results.map(r=>`
   <div class="res">${r.cover_url?`<img class="cover" src="${esc(r.cover_url)}" onerror="this.style.visibility='hidden'" alt="">`:'<div class="cover"></div>'}
   <div class="body"><div class="t">${esc(r.title)}</div><div class="a">${esc(r.author||'')}</div>
   ${starsHTML(r)}${netAvg(r)}
   ${r.mine.map(m=>`<span class="stamp yes">On your shelf · ${esc(m.location||'location unknown')}</span>`).join('')}
   ${r.friends.map(f=>`<div><span class="stamp friend">${esc(f.owner)} has a copy${f.location?' · '+esc(f.location):''}</span><button class="slip" data-borrow="${f.id}">✎ Borrowing slip · Request to borrow</button></div>`).join('')}
   ${!r.mine.length&&!r.friends.length?'<span class="stamp nobody">Not held by any member</span>':''}</div></div>`).join('');
  bindBorrow(el)}
function bindBorrow(el){el.querySelectorAll('[data-borrow]').forEach(b=>b.onclick=async()=>{try{const j=await api('/api/borrow/'+b.dataset.borrow,{method:'POST',body:{}});b.textContent=j.note?'Slip already filed':'Slip filed ✔ awaiting the owner';b.disabled=true}catch(e){alert(e.message)}})}

// ---- star ratings ----
function starsHTML(b,val=b.my_rating){val=val||0;return `<span class="stars" role="group" aria-label="Your rating for ${esc(b.title)}" data-t="${esc(b.title)}" data-a="${esc(b.author||'')}" data-c="${esc(b.cover_url||'')}" data-v="${val}">`+
  [1,2,3,4,5].map(n=>`<button type="button" class="${n<=val?'on':''}" data-n="${n}" aria-label="${n} star${n>1?'s':''}" aria-pressed="${n===val}">★</button>`).join('')+'</span>'}
const netAvg=r=>r.net_avg?`<span class="net-avg">Friends: ${r.net_avg}★ (${r.net_count})</span>`:'';
// Tap a star to rate; tap your current rating again to clear it. Every widget for the same book updates together.
document.addEventListener('click',async e=>{const b=e.target.closest('.stars button');if(!b)return;const w=b.parentElement,n=+b.dataset.n,v=n===+w.dataset.v?0:n;
  try{await api('/api/ratings',{method:'POST',body:{title:w.dataset.t,author:w.dataset.a,cover_url:w.dataset.c,stars:v}});
    document.querySelectorAll('.stars').forEach(o=>{if(o.dataset.t===w.dataset.t&&o.dataset.a===w.dataset.a){o.dataset.v=v;o.querySelectorAll('button').forEach(x=>{x.classList.toggle('on',+x.dataset.n<=v);x.setAttribute('aria-pressed',+x.dataset.n===v)})}})}
  catch(x){alert(x.message)}});

// ---- suggestions (shown on the Catalogue when the search box is empty) ----
async function loadSuggestions(){const el=$('#results');let j;try{j=await api('/api/suggestions')}catch{return}
  if($('#q').value.trim().length>=2)return; // user started typing meanwhile
  const {suggestions:sg,rated}=j;
  el.innerHTML=`<h2 class="sugg-head">Suggested for you</h2>`+(sg.length?sg.map(r=>`
   <div class="res">${r.cover_url?`<img class="cover" src="${esc(r.cover_url)}" onerror="this.style.visibility='hidden'" alt="">`:'<div class="cover"></div>'}
   <div class="body"><div class="t">${esc(r.title)}</div><div class="a">${esc(r.author||'')}</div>
   ${r.reasons.map(x=>`<div class="why">${esc(x)}</div>`).join('')}${netAvg(r)}
   ${r.copies.map(f=>`<div><span class="stamp friend">${esc(f.owner)} has a copy</span><button class="slip" data-borrow="${f.id}">✎ Borrowing slip · Request to borrow</button></div>`).join('')}
   ${r.copies.length?'':'<span class="stamp nobody">Nobody in your network holds a copy</span>'}
   <div class="dim small">Read it already? Rate it: ${starsHTML(r,0)}</div></div></div>`).join(''):
   `<p class="dim">${rated?'No suggestions yet. Rate more books, or add friends so there are more shelves to draw from.':'Rate some books with stars (on My Shelves or in search results) and suggestions from your friends’ shelves will appear here.'}</p>`);
  bindBorrow(el)}

// ---- add / scan ----
async function loadLocs(){const l=await api('/api/locations');$('#locs').innerHTML=l.map(x=>`<option value="${esc(x)}">`).join('')}
function fill(b){const f=$('#bookForm');for(const k of['title','author','isbn','cover_url'])f[k].value=b[k]||'';f.source.value=b.source?'barcode':'manual';
  const c=$('#coverPrev');if(b.cover_url){c.src=b.cover_url;c.classList.remove('hidden')}else c.classList.add('hidden')}
async function lookup(code){const isbn=(code||'').replace(/[^0-9Xx]/g,'');$('#scanMsg').textContent='Consulting the catalogue for '+isbn+'…';
  try{const b=await api('/api/isbn/'+isbn);fill(b);$('#scanMsg').textContent=`Found via ${b.source}. Choose a shelf location and shelve it.`;$('#bookForm').location.focus()}
  catch(e){fill({isbn});$('#scanMsg').textContent=e.message}}
$('#lookupBtn').onclick=()=>lookup($('#isbnIn').value);
$('#bookForm').onsubmit=async e=>{e.preventDefault();const f=e.target,body=Object.fromEntries(new FormData(f));
  try{await api('/api/books',{method:'POST',body});$('#saveMsg').textContent=`✔ Shelved “${body.title}”`;const loc=f.location.value,sh=f.shelf_id.value;f.reset();f.location.value=loc;f.shelf_id.value=sh;$('#coverPrev').classList.add('hidden');boot()}
  catch(err){$('#saveMsg').textContent=err.message}};

// Camera: native BarcodeDetector where available (Android Chrome), html5-qrcode fallback (iOS Safari etc.)
let stream,h5,timer;
async function startScan(){$('#scanMsg').textContent='Opening the camera…';$('#scanBtn').classList.add('hidden');$('#stopBtn').classList.remove('hidden');
  const got=c=>{if(/^(97[89])?\d{9}[\dX]$/i.test(c)){stopScan();lookup(c)}};
  try{if('BarcodeDetector' in window){const det=new BarcodeDetector({formats:['ean_13','ean_8','upc_a']});
      stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});const v=$('#vid');v.srcObject=stream;v.classList.remove('hidden');await v.play();
      $('#scanMsg').textContent='Point at the barcode on the back cover';
      timer=setInterval(async()=>{const r=await det.detect(v).catch(()=>[]);if(r[0])got(r[0].rawValue)},250)}
    else{h5=new Html5Qrcode('reader',{formatsToSupport:[Html5QrcodeSupportedFormats.EAN_13,Html5QrcodeSupportedFormats.EAN_8,Html5QrcodeSupportedFormats.UPC_A]});
      await h5.start({facingMode:'environment'},{fps:10,qrbox:{width:260,height:120}},got);$('#scanMsg').textContent='Point at the barcode on the back cover'}}
  catch(e){$('#scanMsg').textContent='Camera unavailable ('+e.message+'). Type the ISBN instead. (Camera needs https.)';stopScan()}}
function stopScan(){clearInterval(timer);if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}$('#vid').classList.add('hidden');
  if(h5){h5.stop().catch(()=>{});h5=null}$('#scanBtn').classList.remove('hidden');$('#stopBtn').classList.add('hidden')}
$('#scanBtn').onclick=startScan;$('#stopBtn').onclick=stopScan;

// ---- my books ----
async function loadMine(){const b=await api('/api/books');const groups=new Map();
  for(const x of b){const k=x.shelf+'\u0000'+(x.location||'Unshelved');if(!groups.has(k))groups.set(k,[]);groups.get(k).push(x)}
  const sp=['#1f3a2c','#7a1f1f','#2c3e5c','#6b4a2b','#4a3b5c','#8a6a22'];let i=0,h=`<p class="dim">${b.length} volumes across ${groups.size} shelves</p><form id="shelfNameForm" class="shelf-name"><label>Your shelf is called <input name="n" maxlength="60" required></label> <button>Re-letter plate</button> <span id="snMsg" class="dim"></span></form>`;
  for(const [k,xs] of groups){const [sh,loc]=k.split('\u0000');h+=`<div class="shelf"><span class="shelf-plate">${esc(loc)}</span><span class="shelf-sub">${esc(sh)} · ${xs.length}</span><div class="shelf-books">`+
    xs.map(x=>`<div class="mb" style="--sp:${sp[i++%sp.length]}"><span>${esc(x.title)} <span class="a">${esc(x.author||'')}</span>${starsHTML(x,x.my_rating)}</span><button class="ghost" data-del="${x.id}" aria-label="Withdraw ${esc(x.title)}">Withdraw</button></div>`).join('')+`</div></div>`}
  $('#mine').innerHTML=h;api('/api/me').then(m=>{$('#shelfNameForm').n.value=m.shelf_name||''});
  $('#shelfNameForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/my-shelf-name',{method:'POST',body:{name:e.target.n.value}});$('#snMsg').textContent='✔ Plate re-lettered';loadMine();boot()}catch(x){$('#snMsg').textContent=x.message}};$('#mine').querySelectorAll('[data-del]').forEach(d=>d.onclick=async()=>{if(confirm('Withdraw this book from your shelves?')){await api('/api/books/'+d.dataset.del,{method:'DELETE'});loadMine();boot()}})}

// ---- requests ----
async function loadReqs(){const {incoming,outgoing}=await api('/api/requests');
  const row=(r,mine)=>`<div class="mb"><span>${mine?`<b>${esc(r.requester)}</b> wishes to borrow${r.shelf?' from '+esc(r.shelf):''}`:`You asked <b>${esc(r.owner)}</b> to lend`} “${esc(r.title)}” <span class="status">· ${({pending:'awaiting',approved:'lent',declined:'declined'})[r.status]||r.status}</span></span>${mine&&r.status==='pending'?`<span><button data-a="${r.id}" data-s="approved">Lend</button> <button class="ghost" data-a="${r.id}" data-s="declined">Decline</button></span>`:''}</div>`;
  $('#reqs').innerHTML=`<div class="card"><h2>Members asking to borrow</h2>${incoming.map(r=>row(r,1)).join('')||'<p class="dim">None yet.</p>'}</div><div class="card"><h2>Your borrowing slips</h2>${outgoing.map(r=>row(r,0)).join('')||'<p class="dim">None yet.</p>'}</div>`;
  $('#reqs').querySelectorAll('[data-a]').forEach(b=>b.onclick=async()=>{await api('/api/requests/'+b.dataset.a,{method:'POST',body:{status:b.dataset.s}});loadReqs()})}
// ---- invites ----
const invP=new URLSearchParams(location.search).get('invite');if(invP)$('#inv').value=invP.toUpperCase();
async function loadInv(){const [{invites,max_unused},{groups}]=await Promise.all([api('/api/invites'),api('/api/groups')]);
  const sel=$('#invGroup'),prev=sel.value;sel.innerHTML=groups.map(g=>`<option value="${g.id}">${esc(g.name)}</option>`).join('');if(prev)sel.value=prev;$('#newInv').disabled=!groups.length;const base=location.origin+'/?invite=';
  const unused=invites.filter(i=>!i.used_at).length;
  $('#invites').innerHTML=`<p class="dim small">${unused}/${max_unused} invitations outstanding</p>`+(invites.map(i=>i.used_at?
   `<div class="mb dim"><span><s>${esc(i.code)}</s> <span class="a">${esc(i.group_name||'')} · admitted ${esc(i.used_by_name||'?')} · ${esc(i.used_at)}</span></span></div>`:
   `<div class="card"><div class="ticket">${esc(i.code)}</div><div class="a">Unused invitation · ${esc(i.group_name||'no circle')}</div><input readonly value="${esc(base+i.code)}" onclick="this.select()">
    <div class="row"><button data-copy="${esc(base+i.code)}">Copy link</button><button class="ghost" data-rev="${esc(i.code)}">Revoke</button></div></div>`).join('')||'<p class="dim">No invitations issued yet.</p>');
  $('#invites').querySelectorAll('[data-copy]').forEach(b=>b.onclick=async()=>{try{await navigator.clipboard.writeText(b.dataset.copy);b.textContent='Copied ✔'}catch{b.previousElementSibling;prompt('Copy this link:',b.dataset.copy)}});
  $('#invites').querySelectorAll('[data-rev]').forEach(b=>b.onclick=async()=>{if(confirm('Revoke this invitation?')){await api('/api/invites/'+b.dataset.rev,{method:'DELETE'});loadInv()}})}
$('#newInv').onclick=async()=>{$('#invMsg').textContent='';try{await api('/api/invites',{method:'POST',body:{group_id:$('#invGroup').value}});loadInv()}catch(e){$('#invMsg').textContent=e.message}};
// ---- groups ----
async function loadGroups(){const {groups}=await api('/api/groups');
  $('#groups').innerHTML=groups.map(g=>`<div class="card"><h2>${esc(g.name)}</h2><p class="dim small">${g.members.length} members · owner ${esc(g.owner||'?')}</p>`+
    g.members.map(m=>`<div class="mb"><span>${esc(m.username)}</span><span class="a">${m.books} volumes</span></div>`).join('')+`</div>`).join('')||'<p class="dim">You are not in any circle yet.</p>'}
$('#redeemBtn').onclick=async()=>{$('#redeemMsg').textContent='';try{const r=await api('/api/redeem',{method:'POST',body:{code:$('#redeemIn').value}});
  $('#redeemMsg').textContent='✔ Joined '+r.group;$('#redeemIn').value='';loadGroups()}catch(e){$('#redeemMsg').textContent=e.message}};

// ---- friends ----
async function loadFriends(){const {friends,incoming,outgoing}=await api('/api/friends');
  $('#friends').innerHTML=
   (incoming.length?`<div class="card"><h2>Friend requests</h2>${incoming.map(r=>`<div class="mb"><span><b>${esc(r.username)}</b> wants to be friends</span><span><button data-fa="${r.id}" data-act="accept">Accept</button> <button class="ghost" data-fa="${r.id}" data-act="decline">Decline</button></span></div>`).join('')}</div>`:'')+
   `<div class="card"><h2>Your friends</h2>${friends.map(f=>`<div class="mb"><span>${esc(f.username)} <span class="a">· ${esc(f.shelf_name)} · ${f.books} volumes</span></span><button class="ghost" data-unf="${f.id}" data-n="${esc(f.username)}">Unfriend</button></div>`).join('')||'<p class="dim">No friends yet. Send a request above.</p>'}</div>`+
   (outgoing.length?`<div class="card"><h2>Requests you sent</h2>${outgoing.map(r=>`<div class="mb"><span>${esc(r.username)} <span class="status">· awaiting</span></span><button class="ghost" data-unf="${r.id}">Cancel</button></div>`).join('')}</div>`:'');
  $('#friends').querySelectorAll('[data-fa]').forEach(b=>b.onclick=async()=>{await api(`/api/friends/${b.dataset.fa}/${b.dataset.act}`,{method:'POST',body:{}});loadFriends()});
  $('#friends').querySelectorAll('[data-unf]').forEach(b=>b.onclick=async()=>{if(b.dataset.n&&!confirm(`Unfriend ${b.dataset.n}? They will no longer see your shelf (unless you share a circle or book club).`))return;
    await api('/api/friends/'+b.dataset.unf,{method:'DELETE'});loadFriends()})}
async function sendFriend(){$('#friendMsg').textContent='';try{const r=await api('/api/friends',{method:'POST',body:{username:$('#friendIn').value}});
  $('#friendMsg').textContent='✔ '+r.note;$('#friendIn').value='';loadFriends()}catch(e){$('#friendMsg').textContent=e.message}}
$('#friendBtn').onclick=sendFriend;$('#friendIn').onkeydown=e=>{if(e.key==='Enter')sendFriend()};

// ---- mini book clubs (group chats) ----
const seen={get(id){try{return +localStorage.getItem('club-seen-'+id)||0}catch{return 0}},set(id,v){try{localStorage.setItem('club-seen-'+id,v)}catch{}}};
let club=null,chatTimer=null,lastMsg=0;
function stopChat(){clearInterval(chatTimer);chatTimer=null;club=null}
async function showClubList(){stopChat();$('#clubView').classList.add('hidden');$('#clubList').classList.remove('hidden');
  const [{clubs},{friends}]=await Promise.all([api('/api/clubs'),api('/api/friends')]);
  $('#clubList').innerHTML=`<form id="newClub" class="card"><h2>Start a book club</h2><p class="dim small">A small reading group, like a group chat. Members can message each other and see each other's shelves.</p>
     <label class="field"><span>Club name</span><input name="name" maxlength="60" required placeholder="e.g. Sunday Classics"></label>
     <fieldset class="pick"><legend class="field"><span>Invite friends</span></legend>${friends.map(f=>`<label><input type="checkbox" name="m" value="${f.id}"> ${esc(f.username)}</label>`).join('')||'<p class="dim small">Add some friends first on the Friends tab, or start the club on your own and add them later.</p>'}</fieldset>
     <button>Start club</button> <span id="newClubMsg" class="err"></span></form>`+
    (clubs.map(c=>`<button class="club-item" data-club="${c.id}"><span class="club-name">${esc(c.name)}${c.last_id>seen.get(c.id)?' <span class="new-dot" aria-label="new messages">●</span>':''}</span>
      <span class="a">${c.member_count} members · ${esc(c.members.join(', '))}</span>
      ${c.last_body?`<span class="club-last">${c.last_author?esc(c.last_author)+': ':''}${esc(c.last_body.slice(0,90))}</span>`:''}</button>`).join('')||'<p class="dim">You are not in any book clubs yet.</p>');
  $('#newClub').onsubmit=async e=>{e.preventDefault();const f=e.target;try{const r=await api('/api/clubs',{method:'POST',body:{name:f.name.value,members:[...f.querySelectorAll('[name=m]:checked')].map(x=>x.value)}});openClub(r.id)}
    catch(x){$('#newClubMsg').textContent=x.message}};
  $('#clubList').querySelectorAll('[data-club]').forEach(b=>b.onclick=()=>openClub(+b.dataset.club))}
const fmtTime=s=>{const d=new Date(s.replace(' ','T')+'Z');return isNaN(d)?s:d.toLocaleString([], {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})};
function renderMsgs(ms,me,append){if(append)ms=ms.filter(m=>m.id>lastMsg);const log=$('#chatLog'),atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<40;
  const h=ms.map(m=>m.user_id===null?`<div class="msg sys">${esc(m.body)}</div>`:
    `<div class="msg ${m.user_id===me?'me':''}"><div class="msg-meta">${m.user_id===me?'You':esc(m.author)} · ${esc(fmtTime(m.created_at))}</div><div class="msg-body">${esc(m.body)}</div></div>`).join('');
  if(append)log.insertAdjacentHTML('beforeend',h);else log.innerHTML=h||'<p class="dim small">No messages yet. Say hello!</p>';
  if(ms.length)lastMsg=ms[ms.length-1].id;if(!append||atBottom)log.scrollTop=log.scrollHeight;seen.set(club,lastMsg)}
async function openClub(id){stopChat();club=id;lastMsg=0;$('#clubList').classList.add('hidden');$('#clubView').classList.remove('hidden');$('#clubMsg').textContent='';
  let c;try{c=await api('/api/clubs/'+id)}catch(e){return showClubList()}
  $('#clubName').textContent=c.name;$('#clubRenameIn').value=c.name;$('#clubMembers').textContent=c.members.map(m=>m.username).join(', ');renderMsgs(c.messages,c.me,false);
  const {friends}=await api('/api/friends'),ids=new Set(c.members.map(m=>m.id)),avail=friends.filter(f=>!ids.has(f.id));
  $('#clubAddSel').innerHTML=avail.map(f=>`<option value="${f.id}">${esc(f.username)}</option>`).join('')||'<option value="">No other friends to add</option>';$('#clubAddBtn').disabled=!avail.length;
  chatTimer=setInterval(pollClub,4000)}
let polling=false;
async function pollClub(){if(!club||document.hidden||polling)return;const id=club;polling=true;try{const c=await api(`/api/clubs/${id}?after=${lastMsg}`);if(id!==club)return;
  $('#clubName').textContent=c.name;$('#clubMembers').textContent=c.members.map(m=>m.username).join(', ');if(c.messages.length)renderMsgs(c.messages,c.me,lastMsg>0)}catch(e){if(/No such/.test(e.message))showClubList()}finally{polling=false}}
$('#clubBack').onclick=showClubList;
$('#chatForm').onsubmit=async e=>{e.preventDefault();const t=e.target.body,v=t.value.trim();if(!v)return;
  try{await api(`/api/clubs/${club}/messages`,{method:'POST',body:{body:v}});t.value='';pollClub()}catch(x){alert(x.message)}};
$('#chatForm').body.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('#chatForm').requestSubmit()}};
const clubAct=async(path,body)=>{$('#clubMsg').textContent='';try{await api(`/api/clubs/${club}/${path}`,{method:'POST',body});return true}catch(e){$('#clubMsg').textContent=e.message}};
$('#clubAddBtn').onclick=async()=>{if(await clubAct('members',{user_id:$('#clubAddSel').value}))openClub(club)};
$('#clubRenameBtn').onclick=async()=>{if(await clubAct('rename',{name:$('#clubRenameIn').value}))openClub(club)};
$('#clubLeaveBtn').onclick=async()=>{if(confirm('Leave this book club?')&&await clubAct('leave',{}))showClubList()};
boot();
