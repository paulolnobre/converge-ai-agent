/* Local attachment drafts live only in memory; nothing sends before a click. */
(function () {
 "use strict";
 const LIMIT=20*1024*1024;
 function node(tag,text,cls){const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;}
 function button(text,click){const n=node('button',text,'btn attachment-button');n.type='button';n.addEventListener('click',click);return n;}
 // Fixed markup, never built from input: the icon is decoration, the name is aria-label + title.
 const ICONS={
  clip:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
  mic:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg>',
  stop:'<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>'
 };
 function setIcon(n,icon,label){n.innerHTML=ICONS[icon];n.setAttribute('aria-label',label);n.title=label;n.classList.toggle('recording',icon==='stop');}
 function iconButton(icon,label,click){const n=node('button',null,'btn attachment-button attachment-icon');n.type='button';setIcon(n,icon,label);n.addEventListener('click',click);return n;}
 function shortName(name){const chars=Array.from(name);if(chars.length<=120)return name;const ext=(name.match(/\.[a-z0-9]{1,10}$/i)||[''])[0];return chars.slice(0,120-ext.length).join('')+ext;}
 function metadata(value){return btoa(Array.from(new TextEncoder().encode(JSON.stringify(value)),n=>String.fromCharCode(n)).join(''));}
 function renderMedia(media,url){
  const box=node('div',null,'msg-media');
  if(!media.available){box.append(node('span',media.status_text||'Arquivo indisponível.'));return box;}
  if(media.inline && media.kind==='image'){
   const image=node('img',null,'media-img');image.src=url;image.alt='Foto compartilhada';image.loading='lazy';box.append(image);
  }else if(media.inline && media.kind==='audio'){
   const audio=node('audio',null,'media-player');audio.controls=true;audio.preload='none';audio.src=url;box.append(audio);
  }
  const link=node('a','Baixar '+(media.filename||'arquivo'),'btn media-download');link.href=url+'?download=1';box.append(link);
  return box;
 }
 function create(host,options){
  let file=null,kind=null,nonce=null,url=null,recorder=null,stream=null,timer=null,elapsed=0,enabled=false,busy=false,generation=0,asking=false,discard=false;
  const controls=node('div',null,'attachment-controls'), preview=node('div',null,'attachment-preview');
  const note=node('span',null,'attachment-note');note.setAttribute('role','status');
  const input=node('input');input.type='file';input.hidden=true;
  const pick=iconButton('clip','Anexar foto ou arquivo',()=>input.click());
  const mic=iconButton('mic','Gravar áudio',record);
  const remove=button('Descartar',()=>reset());
  const submit=button('Enviar anexo',send);
  controls.append(pick,mic,input,note);host.append(controls,preview);host.classList.add('attachment-editor');host.hidden=true;
  function release(){if(stream)stream.getTracks().forEach(t=>t.stop());stream=null;clearInterval(timer);timer=null;}
  function clearURL(){if(url)URL.revokeObjectURL(url);url=null;}
  function reset(){generation++;discard=true;if(recorder&&recorder.state!=='inactive')recorder.stop();recorder=null;release();asking=false;clearURL();file=null;kind=null;nonce=null;preview.replaceChildren();note.textContent='';setIcon(mic,'mic','Gravar áudio');input.value='';update();}
  function update(){pick.disabled=!enabled||busy||!!recorder||asking;mic.disabled=!enabled||busy||asking;remove.disabled=busy;submit.disabled=!enabled||busy||!!recorder;}
  function select(value,type){
   if(value.size>LIMIT){note.textContent='O arquivo pode ter até 20 MB.';return;}
   clearURL();file=value;kind=type;nonce=crypto.randomUUID();preview.replaceChildren();
   preview.append(node('span',value.name+' · '+Math.max(1,Math.ceil(value.size/1024))+' KB','attachment-filename'));
   url=URL.createObjectURL(value);
   if(type==='audio'){const audio=node('audio');audio.controls=true;audio.src=url;preview.append(audio);}
   if(type==='image'&&/^image\/(jpeg|png|webp)$/.test(value.type)){const image=node('img');image.src=url;image.alt='Prévia da foto';preview.append(image);}
   preview.append(remove,submit);note.textContent='Confira o anexo antes de enviar.';update();
  }
  input.addEventListener('change',()=>{const value=input.files[0];if(value)select(value,value.type.startsWith('image/')?'image':value.type.startsWith('audio/')?'audio':'document');});
  async function record(){
   if(recorder){recorder.stop();return;}
   if(!enabled||busy||asking)return;
   if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){note.textContent='Este navegador não grava áudio. Use o clipe para escolher um áudio.';return;}
   reset();discard=false;asking=true;update();const run=generation;
   try{
    const acquired=await navigator.mediaDevices.getUserMedia({audio:true});
    if(run!==generation||!enabled){acquired.getTracks().forEach(t=>t.stop());return;}
    stream=acquired;
    const mime=['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'].find(type=>MediaRecorder.isTypeSupported(type));
    const active=new MediaRecorder(stream,mime?{mimeType:mime}:{});recorder=active;const chunks=[];let bytes=0;
    active.addEventListener('dataavailable',event=>{if(event.data.size){chunks.push(event.data);bytes+=event.data.size;if(bytes>LIMIT){discard=true;if(active.state==='recording')active.stop();note.textContent='O áudio ficou grande demais. Grave um trecho menor.';}}});
    active.addEventListener('stop',()=>{release();if(recorder===active)recorder=null;setIcon(mic,'mic','Gravar áudio');if(run!==generation||discard){update();return;}const blob=new Blob(chunks,{type:active.mimeType||mime||'audio/webm'});if(blob.size)select(new File([blob],'gravacao.'+(blob.type.includes('mp4')?'m4a':'webm'),{type:blob.type}),'audio');update();});
    active.addEventListener('error',()=>{discard=true;release();recorder=null;note.textContent='Não consegui gravar. Tente novamente.';update();});
    active.start(1000);elapsed=0;setIcon(mic,'stop','Parar gravação');
    note.textContent='Gravando… 0:00';timer=setInterval(()=>{elapsed++;note.textContent='Gravando… '+Math.floor(elapsed/60)+':'+String(elapsed%60).padStart(2,'0');if(elapsed>=180&&active.state==='recording')active.stop();},1000);
   }catch(_){release();recorder=null;note.textContent='Não consegui acessar o microfone. Confira a permissão do navegador ou anexe um áudio.';}
   finally{if(run===generation)asking=false;update();}
  }
  async function send(){
   if(!enabled||busy||!file||recorder)return;
   const caption=(options.caption()||'').trim();
   if(caption.length>500){note.textContent='A legenda pode ter até 500 caracteres.';return;}
   if(kind==='audio'&&caption){note.textContent='Envie o áudio sem legenda. O texto pode ser enviado separadamente.';return;}
   const endpoint=options.url(),run=generation;
   busy=true;update();note.textContent='Enviando anexo…';
   try{
    const response=await fetch(endpoint,{method:'POST',credentials:'same-origin',cache:'no-store',
     headers:{'Content-Type':'application/octet-stream','X-Upload-Metadata':metadata({nonce,kind,filename:shortName(file.name),caption})},body:file});
    const result=await response.json();
    if(response.status===401){window.PanelApp.signOut();throw Error('Entre novamente.');}
    if(!response.ok)throw Error(typeof result.detail==='string'?result.detail:'Não consegui enviar. Tente de novo.');
    if(run===generation){
     if(result.delivery_state==='sent'){reset();options.sent(endpoint,caption);}
     else{note.textContent=result.notice||'Envio não confirmado. Confira antes de tentar novamente.';options.refresh(endpoint);}
    }
   }catch(error){if(run===generation)note.textContent=error.message||'Conexão interrompida. Tente novamente para consultar o mesmo envio.';}
   finally{busy=false;update();}
  }
  document.addEventListener('visibilitychange',()=>{if(document.hidden&&(recorder||asking))reset();});
  window.addEventListener('pagehide',()=>reset());
  document.addEventListener('panel:view',()=>{if(!host.closest('.view')?.classList.contains('active'))reset();});
  return {send,reset,hasFile:()=>!!file,setEnabled:value=>{enabled=!!value;host.hidden=!enabled;if(!enabled&&(file||recorder||asking))reset();update();}};
 }
 window.PanelAttachments={create,renderMedia};
})();
