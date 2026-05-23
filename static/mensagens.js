// ============================================================
// MODAL MENSAGENS PLENÁRIO — lógica isolada para estabilidade
// Depende de: window.ITENS_DATA e window.EVENTO_ID (definidos no template)
// ============================================================

const ementaCache = {};

async function enriquecerEmenta(idPrincipal, projeto, ementa, autor) {
  if (ementaCache[idPrincipal]) return ementaCache[idPrincipal];
  try {
    const r = await fetch('/enriquecer_ementa', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({projeto, ementa, autor})
    });
    const j = await r.json();
    ementaCache[idPrincipal] = j.ementa_enriquecida || ementa;
  } catch(e) {
    ementaCache[idPrincipal] = ementa;
  }
  return ementaCache[idPrincipal];
}

function stripHTML(html) {
  return html
    .replace(/<strong>(.*?)<\/strong>/gi, '*$1*')
    .replace(/<b>(.*?)<\/b>/gi, '*$1*')
    .replace(/<em>(.*?)<\/em>/gi, '_$1_')
    .replace(/<i>(.*?)<\/i>/gi, '_$1_')
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&nbsp;/g, ' ')
    .replace(/\*\*(.*?)\*\*/g, '*$1*')
    .trim();
}

let tipoAtual = null;
let itemAtual = null;

async function gerarMensagem() {
  if (!tipoAtual) return;
  const semItem = ['iniciada', 'encerrada_ordem', 'encerrada_sessao'];
  const itemOpcional = ['resultado_req']; // item é opcional
  if (!semItem.includes(tipoAtual) && !itemOpcional.includes(tipoAtual) && !itemAtual) {
    document.getElementById('preview-msg-container').style.display = 'none';
    return;
  }
  try {
    const ori = itemAtual ? (itemAtual.orientacao || 'A DEFINIR').toUpperCase() : '';
    const obj = document.getElementById('input-objeto')?.value.trim() || '';
    let ementa = itemAtual ? itemAtual.ementa : '';

    let resumo = '';
    if (itemAtual) {
      if (itemAtual.resumo_ia) {
        resumo = itemAtual.resumo_ia;
      } else {
        const elResumo = document.getElementById('resumo-ia-' + itemAtual.id_principal);
        if (elResumo && elResumo.textContent.trim()) {
          resumo = elResumo.textContent.trim();
          itemAtual.resumo_ia = resumo;
        }
      }
    }

    let msg = '';

    if (tipoAtual === 'apresentacao') {
      const linhaProj = resumo ? `*${itemAtual.projeto}* — *${resumo}*` : `*${itemAtual.projeto}*`;
      const ementaCompleta = stripHTML(itemAtual.ementa || '');
      msg = `${linhaProj}\n\n${ementaCompleta}\n\nAutor: ${itemAtual.autor}\nRelator: ${itemAtual.relator}\n\n*OPOSIÇÃO ORIENTA: ${ori}*`;

    } else if (tipoAtual === 'aprovada_simbolica' || tipoAtual === 'aprovado_simbolico') {
      const linhaProj = resumo ? `*${itemAtual.projeto}* — *${resumo}*` : `*${itemAtual.projeto}*`;
      msg = `O ${linhaProj} foi *APROVADO SIMBOLICAMENTE*`;

    } else if (tipoAtual === 'votacao') {
      if (!obj) { document.getElementById('preview-msg-container').style.display = 'none'; return; }
      const descDtq = document.getElementById('input-objeto')?.dataset.descricaoDtq || '';
      const linhaDesc = descDtq ? `\n${descDtq}` : '';
      // Em votação: só o resumo (sem ementa)
      const linhaResumo = resumo ? `*${resumo}*` : `*${itemAtual.projeto}*`;
      msg = `‼ *ATENÇÃO* ‼\n\n*VOTAÇÃO NOMINAL EM PLENÁRIO AGORA*\n\n_${obj}_ - ${itemAtual.projeto}${linhaDesc}\n\n${linhaResumo}\n\n*OPOSIÇÃO ORIENTA: ${ori}*`;

    } else if (tipoAtual === 'iniciada') {
      msg = `🟢 *INICIADA A ORDEM DO DIA*\n\nA Ordem do Dia foi iniciada no Plenário da Câmara dos Deputados.`;

    } else if (tipoAtual === 'encerrada_ordem') {
      msg = `🔴 *ENCERRADA A ORDEM DO DIA*\n\nA Ordem do Dia foi encerrada no Plenário da Câmara dos Deputados.`;

    } else if (tipoAtual === 'encerrada_sessao') {
      msg = `⛔ *ENCERRADA A SESSÃO DELIBERATIVA*\n\nA Sessão Deliberativa foi encerrada no Plenário da Câmara dos Deputados.`;

    } else if (tipoAtual === 'resultado_req') {
      const req = window._reqAtual || {tipo: 'Requerimento', resultado: 'aprovado_simbolico'};
      const proj = itemAtual ? `do *${itemAtual.projeto}*` : '';
      const linhaResumo = resumo ? `\n*${resumo}*` : '';
      if (req.resultado === 'aprovado_simbolico') {
        msg = `✅ O Requerimento de *${req.tipo}* ${proj} foi *APROVADO SIMBOLICAMENTE*${linhaResumo}`;
      } else if (req.resultado === 'aprovado_nominal') {
        const v = window._votosAtuais || {sim:'', nao:'', abs:''};
        const linhaAbs = v.abs ? `\n*Abstenção*: ${v.abs} votos` : '';
        msg = `✅ O Requerimento de *${req.tipo}* ${proj} foi *APROVADO*${linhaResumo}\n*SIM*: ${v.sim} votos\n*NÃO*: ${v.nao} votos${linhaAbs}`;
      } else {
        const v = window._votosAtuais || {sim:'', nao:'', abs:''};
        const linhaAbs = v.abs ? `\n*Abstenção*: ${v.abs} votos` : '';
        msg = `❌ O Requerimento de *${req.tipo}* ${proj} foi *REJEITADO*${linhaResumo}\n*SIM*: ${v.sim} votos\n*NÃO*: ${v.nao} votos${linhaAbs}`;
      }

    } else if (tipoAtual === 'aprovado_nominal' || tipoAtual === 'rejeitado_nominal') {
      const v = window._votosAtuais || {sim:'', nao:'', abs:'', resultado: tipoAtual === 'aprovado_nominal' ? 'APROVADO' : 'REJEITADO'};
      const linhaAbs = v.abs ? `\n*Abstenção*: ${v.abs} votos` : '';
      const emojiNom = tipoAtual === 'aprovado_nominal' ? '✅' : '❌';
      const linhaResumo = resumo ? `*${resumo}*` : '';
      msg = `${emojiNom} O *${itemAtual.projeto}* foi *${v.resultado}*` +
            (linhaResumo ? `\n${linhaResumo}` : '') +
            `\n*SIM*: ${v.sim} votos\n*NÃO*: ${v.nao} votos${linhaAbs}`;
    }

    if (msg) {
      document.getElementById('preview-msg').value = msg;
      document.getElementById('preview-msg-container').style.display = 'block';
    }
  } catch(e) {
    console.error('Erro gerarMensagem:', e);
  }
}

function inicializarModalMensagens() {
  const itensData = window.ITENS_DATA || [];
  const eventoId  = window.EVENTO_ID  || 0;

  // Select item
  const selItem = document.getElementById('select-item-msg');
  if (!selItem) return;

  selItem.addEventListener('change', function() {
    const idx = this.value;
    itemAtual = idx !== '' ? itensData[parseInt(idx)] : null;
    dtqCarregado = false;
    const menuDtq = document.getElementById('menu-dtq');
    if (menuDtq) menuDtq.innerHTML = `<li><span class="dropdown-item text-muted fst-italic" id="dtq-loading">
      <span class="spinner-border spinner-border-sm me-1"></span>Buscando destaques...
    </span></li>`;
    const btnDtq = document.getElementById('btnDTQ');
    if (btnDtq) { btnDtq.classList.remove('active','btn-secondary'); btnDtq.classList.add('btn-outline-secondary'); }
    gerarMensagem();
  });

  // Botões tipo mensagem
  document.querySelectorAll('.btn-tipo-msg').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      document.querySelectorAll('.btn-tipo-msg').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      tipoAtual = btn.dataset.tipo;
      const campoObj = document.getElementById('campo-objeto');
      const selectContainer = document.getElementById('select-item-container');
      const semItem = ['iniciada', 'encerrada_ordem', 'encerrada_sessao'];
      if (selectContainer) selectContainer.style.display = semItem.includes(tipoAtual) ? 'none' : 'block';
      if (campoObj) campoObj.style.display = tipoAtual === 'votacao' ? 'block' : 'none';
      // Se vier com objeto fixo (dropdown de votação nominal)
      const objetoFixo = btn.dataset.objetoFixo;
      if (tipoAtual === 'votacao' && objetoFixo !== undefined) {
        const inputObj = document.getElementById('input-objeto');
        if (inputObj) { inputObj.value = objetoFixo; inputObj.dataset.descricaoDtq = ''; }
        if (campoObj) campoObj.style.display = 'block';
      } else if (tipoAtual !== 'votacao') {
        const inputObj = document.getElementById('input-objeto');
        if (inputObj) { inputObj.value = ''; inputObj.dataset.descricaoDtq = ''; }
      }
      gerarMensagem();
    });
  });

  // Botão Resultado Requerimento
  document.querySelectorAll('.btn-resultado-req').forEach(link => {
    link.addEventListener('click', async (e) => {
      e.preventDefault();
      const tipoReq = link.dataset.req;
      const tipoRes = link.dataset.tipo; // aprovado_simbolico, aprovado_nominal, rejeitado_nominal
      tipoAtual = 'resultado_req';
      window._reqAtual = { tipo: tipoReq, resultado: tipoRes };
      window._votosAtuais = null;

      if (tipoRes === 'aprovado_nominal' || tipoRes === 'rejeitado_nominal') {
        const sim = prompt('Votos SIM:') || '';
        const nao = prompt('Votos NÃO:') || '';
        window._votosAtuais = {sim, nao, abs: ''};
      }
      await gerarMensagem();
    });
  });

  // Botões objeto
  document.querySelectorAll('.btn-objeto').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.btn-objeto').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const inp = document.getElementById('input-objeto');
      if (inp) inp.value = btn.dataset.objeto;
      gerarMensagem();
    });
  });

  // Botões requerimento
  document.querySelectorAll('.btn-objeto-req').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      document.querySelectorAll('.btn-objeto').forEach(b => b.classList.remove('active'));
      const btnReq = document.getElementById('btnRequerimento');
      if (btnReq) { btnReq.classList.add('active','btn-secondary'); btnReq.classList.remove('btn-outline-secondary'); }
      const inp = document.getElementById('input-objeto');
      if (inp) inp.value = link.dataset.objeto;
      gerarMensagem();
    });
  });

  // Input objeto manual
  const inputObj = document.getElementById('input-objeto');
  if (inputObj) inputObj.addEventListener('input', gerarMensagem);

  // Copiar mensagem
  const btnCopiar = document.getElementById('btn-copiar-msg');
  if (btnCopiar) btnCopiar.addEventListener('click', () => {
    const txt = document.getElementById('preview-msg')?.value || '';
    navigator.clipboard.writeText(txt).then(() => {
      const el = document.getElementById('copiado-msg');
      if (el) { el.style.display = 'inline'; setTimeout(() => { el.style.display = 'none'; }, 2500); }
    });
  });

  // Dropdown Aprovado
  document.querySelectorAll('.btn-aprovado-tipo').forEach(link => {
    link.addEventListener('click', async (e) => {
      e.preventDefault();
      if (!itemAtual && link.dataset.tipo !== 'aprovado_simbolico') { alert('Selecione um item primeiro.'); return; }
      const tipo = link.dataset.tipo;
      if (tipo === 'aprovado_simbolico') { tipoAtual = 'aprovado_simbolico'; await gerarMensagem(); return; }
      const txtOriginal = link.textContent;
      link.textContent = '⏳ Buscando votos...';
      try {
        const r = await fetch(`/buscar_votos/${eventoId}`);
        const j = await r.json();
        let vot = null;
        if (j.votacoes && j.votacoes.length > 0) {
          const projNorm = itemAtual.projeto.replace(/\D/g, '');
          vot = j.votacoes.find(v => v.proposicao && v.proposicao.replace(/\D/g,'').includes(projNorm)) || j.votacoes[0];
        }
        if (vot && (vot.sim || vot.nao)) {
          tipoAtual = tipo;
          window._votosAtuais = {sim: vot.sim, nao: vot.nao, abs: vot.abstencao, resultado: tipo === 'aprovado_nominal' ? 'APROVADO' : 'REJEITADO'};
        } else {
          const sim = prompt('Votos SIM:') || '';
          const nao = prompt('Votos NÃO:') || '';
          tipoAtual = tipo;
          window._votosAtuais = {sim, nao, abs: '', resultado: tipo === 'aprovado_nominal' ? 'APROVADO' : 'REJEITADO'};
        }
        await gerarMensagem();
      } catch(err) { alert('Erro: ' + err.message); }
      finally { link.textContent = txtOriginal; }
    });
  });

  // Reset modal ao fechar
  const modal = document.getElementById('modalMensagens');
  if (modal) modal.addEventListener('hidden.bs.modal', () => {
    tipoAtual = null; itemAtual = null;
    selItem.value = '';
    const campoObj = document.getElementById('campo-objeto');
    const previewCont = document.getElementById('preview-msg-container');
    const inputObjEl = document.getElementById('input-objeto');
    if (campoObj) campoObj.style.display = 'none';
    if (previewCont) previewCont.style.display = 'none';
    if (inputObjEl) inputObjEl.value = '';
    document.querySelectorAll('.btn-tipo-msg, .btn-objeto').forEach(b => b.classList.remove('active'));
  });

  // DTQ
  let dtqCarregado = false;
  window.carregarDestaquesDTQ = async function() {
    if (!itemAtual || dtqCarregado) return;
    const menu = document.getElementById('menu-dtq');
    const sel2 = document.getElementById('select-item-msg');
    const opt  = sel2?.options[sel2.selectedIndex];
    if (!opt || !opt.value) return;
    const idx = parseInt(opt.value);
    const idProp = itensData[idx]?.id_principal;
    if (!idProp) { if (menu) menu.innerHTML = '<li><span class="dropdown-item text-muted">ID não encontrado.</span></li>'; return; }
    try {
      const r = await fetch(`/destaques/${idProp}`);
      const j = await r.json();
      if (!menu) return;
      menu.innerHTML = '';
      if (j.destaques && j.destaques.length > 0) {
        const liGen = document.createElement('li');
        liGen.innerHTML = `<a class="dropdown-item btn-objeto-dtq" href="#" data-objeto="DTQ" data-descricao="">DTQ (genérico)</a>`;
        menu.appendChild(liGen);
        menu.appendChild(Object.assign(document.createElement('li'), {innerHTML: '<hr class="dropdown-divider">'}));
        j.destaques.forEach(d => {
          const li = document.createElement('li');
          li.innerHTML = `<a class="dropdown-item btn-objeto-dtq" href="#" data-objeto="DTQ" data-numero="${d.numero}" data-descricao="${d.descricao.replace(/"/g,"'")}" data-autoria="${d.autoria.replace(/"/g,"'")}">
            <strong>${d.numero}</strong> — ${d.autoria}<br>
            <small class="text-muted">${d.descricao.substring(0,80)}${d.descricao.length>80?'...':''}</small></a>`;
          menu.appendChild(li);
        });
      } else {
        menu.innerHTML = `<li><span class="dropdown-item text-muted fst-italic">Nenhum destaque.<br>
          <a class="dropdown-item btn-objeto-dtq" href="#" data-objeto="DTQ" data-descricao="">DTQ genérico</a></span></li>`;
      }
      menu.querySelectorAll('.btn-objeto-dtq').forEach(link => {
        link.addEventListener('click', (e) => {
          e.preventDefault();
          const num = link.dataset.numero || '';
          const desc = link.dataset.descricao || '';
          const aut  = link.dataset.autoria || '';
          const inpObj = document.getElementById('input-objeto');
          if (inpObj) { inpObj.value = num ? `DTQ ${num} (${aut})` : 'DTQ'; inpObj.dataset.descricaoDtq = desc; }
          const btnDtq2 = document.getElementById('btnDTQ');
          if (btnDtq2) { btnDtq2.classList.add('active','btn-secondary'); btnDtq2.classList.remove('btn-outline-secondary'); }
          document.querySelectorAll('.btn-objeto').forEach(b => b.classList.remove('active'));
          gerarMensagem();
        });
      });
      dtqCarregado = true;
    } catch(e) { if (menu) menu.innerHTML = '<li><span class="dropdown-item text-danger">Erro ao buscar.</span></li>'; }
  };
}

// Inicializa quando DOM estiver pronto
document.addEventListener('DOMContentLoaded', inicializarModalMensagens);
