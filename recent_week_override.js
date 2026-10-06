
<script>
/* Force Most Recent Week section to use pillar-based negativity */
(function() {
  function waitForEl(id, cb, tries) {
    tries = tries || 0;
    var el = document.getElementById(id);
    if (el) { cb(el); }
    else if (tries < 20) { setTimeout(function(){ waitForEl(id,cb,tries+1); }, 300); }
  }

  window.addEventListener('load', function() {
    setTimeout(function() {
      if (typeof TRANSCRIPTS === 'undefined' || TRANSCRIPTS.length < 2) return;
      var rows = TRANSCRIPTS.slice(1);
      var NEG_PILLARS = ["Support Quality","App / Tech Issues","Partner and External Delays","Compensation"];
      var total=rows.length, neg=0, pos=0, res=0, ios=0;
      var pillarNeg={}, pillarTotal={}, mktNeg={}, mktTotal={};
      var dates = rows.map(function(r){return new Date(r[2]);}).filter(function(d){return !isNaN(d);});
      if(!dates.length) return;
      var maxDate = new Date(Math.max.apply(null,dates));
      var day = maxDate.getDay();
      var monday = new Date(maxDate); monday.setDate(maxDate.getDate()-((day===0?7:day)-1)); monday.setHours(0,0,0,0);
      var sunday = new Date(monday); sunday.setDate(monday.getDate()+6); sunday.setHours(23,59,59,999);
      var wkRows=[], allRows=rows;
      rows.forEach(function(r){ var d=new Date(r[2]); if(!isNaN(d)&&d>=monday&&d<=sunday) wkRows.push(r); });
      if(!wkRows.length) return;

      function calcMetrics(arr) {
        var t=arr.length, n=0, p=0, r2=0, io=0;
        var pn={}, pt={}, mn={}, mt={};
        arr.forEach(function(row) {
          var pillar=row[0], mkt=row[3], plat=row[4], q1=row[5], q2=row[6];
          // Use pillar for negativity: P1-4 = negative, P5 = positive
          var isNeg = NEG_PILLARS.indexOf(pillar)>=0;
          var isPos = !isNeg;
          var isRes = q2==='Yes';
          var isIos = plat==='iOS';
          if(isNeg) n++; if(isPos) p++; if(isRes) r2++; if(isIos) io++;
          pt[pillar]=(pt[pillar]||0)+1;
          if(isNeg){pn[pillar]=(pn[pillar]||0)+1;}
          mt[mkt]=(mt[mkt]||0)+1;
          if(isNeg){mn[mkt]=(mn[mkt]||0)+1;}
        });
        return {total:t,neg:n,pos:p,res:r2,ios:io,pn:pn,pt:pt,mn:mn,mt:mt};
      }

      var wk=calcMetrics(wkRows), ov=calcMetrics(allRows);
      function pct(n,d){return d===0?0:Math.round(n/d*1000)/10;}
      function pp(a,b){return Math.round((a-b)*10)/10;}
      function delt(d,better){
        var col=better?(d>0?'#16A34A':'#DC2626'):(d<0?'#16A34A':'#DC2626');
        var arr=d>0?'▲':'▼';
        return '<span style="color:'+col+';font-size:.78rem;font-weight:600;">'+arr+' '+Math.abs(d).toFixed(1)+'pp</span>';
      }
      function row(lbl,wv,ovv,d,better) {
        var fmt=function(v){return v.toFixed(1)+'%';};
        return '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #E5E7EB;">'
          +'<span style="font-size:.8rem;">'+lbl+'</span>'
          +'<span style="display:flex;gap:8px;align-items:center;">'
          +'<b style="font-size:.85rem;">'+fmt(wv)+'</b>'
          +'<span style="color:#9CA3AF;font-size:.75rem;">ov:'+fmt(ovv)+'</span>'
          +delt(d,better)+'</span></div>';
      }

      var wkNeg=pct(wk.neg,wk.total), ovNeg=pct(ov.neg,ov.total);
      var wkPos=pct(wk.pos,wk.total), ovPos=pct(ov.pos,ov.total);
      var wkRes=pct(wk.res,wk.total), ovRes=pct(ov.res,ov.total);

      // Update KPI section
      waitForEl('rw-kpis', function(el) {
        el.innerHTML = row('Q3 Transcripts (count)',wk.total,ov.total,0,true)
          .replace('toFixed(1)+','toLocaleString()+')
          + row('Neg % (P1-4 writers)',wkNeg,ovNeg,pp(wkNeg,ovNeg),false)
          + row('Pos % (P5 writers)',wkPos,ovPos,pp(wkPos,ovPos),true)
          + row('Resolution Rate',wkRes,ovRes,pp(wkRes,ovRes),true)
          + row('iOS share',pct(wk.ios,wk.total),pct(ov.ios,ov.total),pp(pct(wk.ios,wk.total),pct(ov.ios,ov.total)),false)
          + row('Android share',pct(wk.total-wk.ios,wk.total),pct(ov.total-ov.ios,ov.total),pp(pct(wk.total-wk.ios,wk.total),pct(ov.total-ov.ios,ov.total)),false);
      });

      // Update Neg by Pillar
      var PCOL={"Support Quality":"#E84855","App / Tech Issues":"#F4A261","Partner and External Delays":"#2A9D8F","Compensation":"#2A6496"};
      var PILLARS=["Support Quality","App / Tech Issues","Partner and External Delays","Compensation"];
      waitForEl('rw-pillars', function(el) {
        var h='';
        PILLARS.forEach(function(p){
          var wP=pct(wk.pn[p]||0,wk.pt[p]||0), oP=pct(ov.pn[p]||0,ov.pt[p]||0);
          h+='<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #E5E7EB;">'
            +'<span style="display:flex;align-items:center;gap:5px;font-size:.8rem;">'
            +'<span style="width:9px;height:9px;border-radius:50%;background:'+(PCOL[p]||'#666')+';display:inline-block;"></span>'+p+'</span>'
            +'<span style="display:flex;gap:6px;align-items:center;">'
            +'<b style="font-size:.82rem;color:'+(PCOL[p]||'#666')+';">'+wP.toFixed(1)+'% neg</b>'
            +'<span style="color:#9CA3AF;font-size:.75rem;">ov:'+oP.toFixed(1)+'%</span>'
            +delt(pp(wP,oP),false)+'</span></div>';
        });
        el.innerHTML=h;
      });

      // Update Neg by Market
      waitForEl('rw-markets', function(el) {
        var mkts=Object.keys(ov.mt).sort(function(a,b){return pct(wk.mn[b]||0,wk.mt[b]||0)-pct(wk.mn[a]||0,wk.mt[a]||0);});
        var h='';
        mkts.forEach(function(m){
          if(!(wk.mt[m])) return;
          var wP=pct(wk.mn[m]||0,wk.mt[m]), oP=pct(ov.mn[m]||0,ov.mt[m]);
          var col=wP>=22?'#DC2626':wP>=18?'#D97706':'#16A34A';
          h+='<div style="padding:4px 0;border-bottom:1px solid #E5E7EB;display:flex;justify-content:space-between;align-items:center;">'
            +'<b style="font-size:.8rem;width:30px;">'+m+'</b>'
            +'<div style="flex:1;margin:0 6px;background:#E5E7EB;border-radius:3px;height:5px;">'
            +'<div style="width:'+Math.min(wP,100)+'%;background:'+col+';border-radius:3px;height:5px;"></div></div>'
            +'<b style="font-size:.82rem;">'+wP.toFixed(1)+'%</b>'
            +'<span style="color:#9CA3AF;font-size:.74rem;margin-left:4px;">ov:'+oP.toFixed(1)+'%</span>'
            +delt(pp(wP,oP),false)+'</div>';
        });
        el.innerHTML=h||'<span style="color:#9CA3AF;font-size:.8rem;">No data</span>';
      });

      // Update subtitle
      waitForEl('rw-subtitle', function(el){
        el.innerHTML='<b>'+wkRows.length+'</b> Q3 transcripts this week &nbsp;|&nbsp; <b>'+allRows.length+'</b> Q3 overall &nbsp;|&nbsp; Neg = P1-4 complaint writers';
      });

    }, 800);
  });
})();
</script>
