/**
 * MindWeave (心络) — Production Web Application
 * Real FastAPI + PostgreSQL 15 + Gemini 2.5 Flash Integration
 */

const VALID_EDITIONS = ['cotton', 'dotgrid', 'parchment', 'traveler', 'hobonichi', 'museglass'];
const VALID_CUPID_LAYOUTS = ['a', 'b', 'c', 'd'];
const VALID_TIERS = ['gold', 'silver', 'crystal'];

const MUSE_CUPID_PORTRAITS = {
  a: './assets/cupid/muse_cupid_a_listen.webp?v=200',
  b: './assets/cupid/muse_cupid_b_notebook.webp?v=200',
  c: './assets/cupid/muse_cupid_c_heartarrow.webp?v=200',
  d: './assets/cupid/muse_cupid_d_plush.webp?v=200',
};

const ARROW_PIN_IMAGES = {
  gold: './assets/cupid/arrow_pin_gold.webp?v=200',
  silver: './assets/cupid/arrow_pin_silver.webp?v=200',
  crystal: './assets/cupid/arrow_pin_crystal.webp?v=200',
};

const CUPID_COMPANION_IMAGES = Object.values(MUSE_CUPID_PORTRAITS);

const ARROW_SVGS = {
  gold: `
    <svg viewBox="0 0 36 22" fill="none" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="goldArrowGrad" x1="0" y1="0" x2="36" y2="22" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#FCE49C"/>
          <stop offset="48%" stop-color="#D99B38"/>
          <stop offset="100%" stop-color="#9E6816"/>
        </linearGradient>
      </defs>
      <line x1="4" y1="11" x2="27" y2="11" stroke="url(#goldArrowGrad)" stroke-width="2.1" stroke-linecap="round"/>
      <path d="M4 11L1.5 6.8L6.8 9.2M4 11L1.5 15.2L6.8 12.8M8.2 11L5.8 7.2M8.2 11L5.8 14.8" stroke="url(#goldArrowGrad)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M33.5 11C30.6 7.4 26.2 6.2 24.8 8.7C24.2 9.8 25.1 11 25.1 11C25.1 11 24.2 12.2 24.8 13.3C26.2 15.8 30.6 14.6 33.5 11Z" fill="url(#goldArrowGrad)"/>
    </svg>
  `,
  silver: `
    <svg viewBox="0 0 36 22" fill="none" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="silverArrowGrad" x1="0" y1="0" x2="36" y2="22" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#EEF5FC"/>
          <stop offset="50%" stop-color="#8FA6BD"/>
          <stop offset="100%" stop-color="#51687E"/>
        </linearGradient>
      </defs>
      <line x1="4" y1="11" x2="27" y2="11" stroke="url(#silverArrowGrad)" stroke-width="2.1" stroke-linecap="round"/>
      <path d="M4.5 11L1.8 7L7.5 11L1.8 15L4.5 11ZM8.5 11L6.2 7.5M8.5 11L6.2 14.5" stroke="url(#silverArrowGrad)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      <polygon points="34,11 24.5,6.3 26.8,11 24.5,15.7" fill="url(#silverArrowGrad)"/>
    </svg>
  `,
  crystal: `
    <svg viewBox="0 0 36 22" fill="none" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="crystalPrismTop" x1="18" y1="3" x2="35" y2="12" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#B8FAFF"/>
          <stop offset="52%" stop-color="#58B8FF"/>
          <stop offset="100%" stop-color="#A67CFF"/>
        </linearGradient>
        <linearGradient id="crystalPrismBot" x1="18" y1="11" x2="35" y2="19" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#76D8FF"/>
          <stop offset="52%" stop-color="#B589FF"/>
          <stop offset="100%" stop-color="#FF96D5"/>
        </linearGradient>
        <linearGradient id="crystalShaftGrad" x1="2" y1="6" x2="26" y2="16" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#7AE8FF"/>
          <stop offset="55%" stop-color="#6EA8FF"/>
          <stop offset="100%" stop-color="#C28EFF"/>
        </linearGradient>
      </defs>
      <line x1="3.8" y1="11" x2="22.5" y2="11" stroke="url(#crystalShaftGrad)" stroke-width="2.2" stroke-linecap="round"/>
      <polygon points="2.2,11 6.2,6.2 9.4,11 6.2,15.8" fill="url(#crystalPrismTop)" opacity="0.92"/>
      <polygon points="6.6,11 9.8,7.2 12.4,11 9.8,14.8" fill="url(#crystalPrismBot)" opacity="0.88"/>
      <polygon points="34.8,11 25.8,4.6 21.4,11" fill="url(#crystalPrismTop)"/>
      <polygon points="34.8,11 25.8,17.4 21.4,11" fill="url(#crystalPrismBot)"/>
      <polygon points="34.8,11 25.8,4.6 21.4,11 25.8,17.4" stroke="#5B8CE8" stroke-width="0.75" stroke-linejoin="round"/>
      <line x1="21.8" y1="11" x2="34.2" y2="11" stroke="#FFFFFF" stroke-width="1.05"/>
      <circle cx="27.2" cy="8.2" r="1.05" fill="#FFFFFF"/>
    </svg>
  `,
};

const CASUAL_DIRECTORY_SVG = `
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
    <path d="M4 6.5h11.5a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4z"/>
    <path d="M17.5 8.5H20a1 1 0 0 1 1 1v2h-3.5"/>
    <path d="M17.5 13.5H20a1 1 0 0 1 1 1v2h-3.5"/>
    <circle cx="9.8" cy="11.2" r="2.1"/>
    <path d="M6.8 16.4c.6-1.5 1.7-2.2 3-2.2s2.4.7 3 2.2"/>
  </svg>
`;

const STONE_STATES = [
  { index: 0, sliderPos: 12, frameIndex: 1, word: 'Bad', dotIndex: 1 },
  { index: 1, sliderPos: 38, frameIndex: 11, word: 'Tired', dotIndex: 4 },
  { index: 2, sliderPos: 64, frameIndex: 16, word: 'Okay', dotIndex: 8 },
  { index: 3, sliderPos: 89, frameIndex: 30, word: 'Great', dotIndex: 11 },
];

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/* ==========================================================================
   Curated First-Open Seed Templates (Persisted into PostgreSQL for new users)
   ========================================================================== */
const INITIAL_SEED_PROFILES = [
  {
    slug: 'chloe',
    nameCn: '沈清秋',
    monogram: '沈',
    roleTag: '心动伴侣',
    contextSub: '四月海风 · 镰仓木栈道',
    tier: 'gold',
    shortMemo: '偏爱温热桂花燕麦拿铁，生理期前三天忌冰美式。',
    traits: [
      '每年四月都想看一次海，偏爱安静不打卡的街巷步道',
      '生理期前三天忌冰美式，必点半糖温热桂花燕麦拿铁',
      '拍照喜欢自然抓拍与拍立得实相纸质感',
    ],
    visualTokens: {
      sealBg: 'linear-gradient(145deg, #fcf4e6 0%, #efd9b8 100%)',
      sealColor: '#694418',
      tapeColor: 'rgba(226, 182, 118, 0.76)',
      tabColor: '#D49A45',
      folioDate: 'APR 17',
      hoboDay: '17',
      hoboMonthWk: 'APR · THU',
      ticketNo: 'NO. 0417-A',
      captionCotton: 'Kamakura Coast · 海风与木栈道',
      captionFilm: 'KODAK PORTRA 400 · 17A',
      noteSign: '— 四月十七 · 午后手札',
      voiceDur: '0:18',
      stampCode: 'KAMAKURA · 04.17',
      fallbackAvatar: './assets/cupid/avatar_chloe.webp',
      fallbackMainPhoto: './assets/photos/photo_9.jpg',
      fallbackSubPhoto: './assets/photos/photo_12.jpg',
    },
    materials: [
      {
        kind: 'note',
        title: '温热桂花燕麦拿铁手札',
        body: '她说每年四月都想来吹一次海风；生理期前三天忌冰美式，要记得点街角那杯温热的桂花燕麦拿铁。',
        snippet: '生理期前三天忌冰美式，半糖温热燕麦奶',
        stampCode: 'KAMAKURA · 04.17',
      },
      {
        kind: 'note',
        title: '镰仓海岸木栈道约定',
        body: '周五午后她想穿米白色针织开衫去海边看海鸥，随身备好拍立得相纸与薄羊绒围巾。',
        snippet: '海边看海鸥 · 备拍立得相纸与薄羊绒围巾',
        stampCode: 'COAST · 04.17',
      },
    ],
  },
  {
    slug: 'xubai',
    nameCn: '周叙白',
    monogram: '周',
    roleTag: '十年挚友',
    contextSub: '老友默契 · 街角黑胶店',
    tier: 'gold',
    shortMemo: '常喝深烘曼特宁手冲，5月12日生日偏爱泥煤威士忌。',
    traits: [
      '黑胶咖啡馆常坐靠窗第二桌，固定点深烘曼特宁',
      '上月搬家全程帮忙，首版《Blue》黑胶周末当面送他',
      '5月12日生日前提前订好藏酒阁泥煤威士忌座位',
    ],
    visualTokens: {
      sealBg: 'linear-gradient(145deg, #f8f1e5 0%, #e7d4b8 100%)',
      sealColor: '#5c3f1c',
      tapeColor: 'rgba(208, 168, 114, 0.75)',
      tabColor: '#C48A3F',
      folioDate: 'MAY 12',
      hoboDay: '12',
      hoboMonthWk: 'MAY · TUE',
      ticketNo: 'NO. 0512-B',
      captionCotton: 'Vinyl & Pour-Over · 十年挚友',
      captionFilm: 'ILFORD HP5 PLUS · 24A',
      noteSign: '— 老友记 · 黑胶咖啡馆',
      voiceDur: '0:14',
      stampCode: 'VINYL CLUB · 05.12',
      fallbackAvatar: './assets/cupid/avatar_xubai.webp',
      fallbackMainPhoto: './assets/photos/cinema_cafe_film.jpg',
      fallbackSubPhoto: './assets/photos/photo_20.jpg',
    },
    materials: [
      {
        kind: 'note',
        title: '首版《Blue》黑胶与生辰笺',
        body: '上月搬家叙白忙前忙后，周末碰面把淘到的首版黑胶《Blue》顺手带给他，五月十二日生日前订好威士忌吧。',
        snippet: '5月12日生日前订好藏酒阁泥煤威士忌座位',
        stampCode: 'VINYL CLUB · 05.12',
      },
    ],
  },
  {
    slug: 'zhixia',
    nameCn: '林知夏',
    monogram: '林',
    roleTag: '跨组同事',
    contextSub: '职场护界 · 留痕对接',
    tier: 'silver',
    shortMemo: '周五临下班惯用夸赞话术推活，只发标准母版留档不兜底。',
    traits: [
      '惯在周五傍晚先夸“你审美最好”再顺势转嫁加班活',
      '所有对接一律走大群公开文档留痕，绝不私聊口头答应',
      '周五 18:00 前发空白母版并@各自负责人即止',
    ],
    visualTokens: {
      sealBg: 'linear-gradient(145deg, #eef3f8 0%, #d5e0ec 100%)',
      sealColor: '#374d63',
      tapeColor: 'rgba(148, 172, 196, 0.76)',
      tabColor: '#6F88A2',
      folioDate: 'APR 19',
      hoboDay: '19',
      hoboMonthWk: 'APR · SAT',
      ticketNo: 'NO. 0419-S',
      captionCotton: 'Rainy Window · 边界备忘留档',
      captionFilm: 'CINESTILL 800T · 09B',
      noteSign: '— 银箭护界 · 冷静留档',
      voiceDur: '0:11',
      stampCode: 'BOUNDARY · 04.19',
      fallbackAvatar: './assets/cupid/avatar_zhixia.webp',
      fallbackMainPhoto: './assets/photos/cinema_rain_glass.jpg',
      fallbackSubPhoto: './assets/photos/cinema_forest_mist.jpg',
    },
    materials: [
      {
        kind: 'note',
        title: '周五临下班推活话术留档',
        body: '周五临下班前习惯用高赞美话术临时推活。往后只同步标准模板留档，周末绝不替他人越界兜底。',
        snippet: '只同步公开母版留档，周末不替他人越界兜底',
        stampCode: 'BOUNDARY · 04.19',
      },
    ],
  },
  {
    slug: 'prof',
    nameCn: '陈教授',
    monogram: '陈',
    roleTag: '论文导师',
    contextSub: '学术长辈 · 学院南楼 402',
    tier: 'crystal',
    shortMemo: '偏爱武夷岩茶，邮件忌寒暄，组会汇报先给三行结论。',
    traits: [
      '每周二上午 10 点前查收邮件最快，忌发长语音',
      '汇报偏好开门见山：正文前三行直接列出核心结论',
      '日常品茗偏爱武夷岩茶（大红袍 / 肉桂）',
    ],
    visualTokens: {
      sealBg: 'linear-gradient(145deg, #f9f2e7 0%, #ead8be 100%)',
      sealColor: '#5c4224',
      tapeColor: 'rgba(206, 178, 138, 0.76)',
      tabColor: '#C59B6D',
      folioDate: 'APR 11',
      hoboDay: '11',
      hoboMonthWk: 'APR · FRI',
      ticketNo: 'NO. 0411-C',
      noteSign: '— 水晶箭手札 · 学术分寸',
      voiceDur: '0:12',
      stampCode: 'ACADEMY · 04.11',
    },
    materials: [
      {
        kind: 'note',
        title: '陈教授 · 邮件与汇报偏好卡',
        body: '上周去南楼 402 找陈老师签字，顺手带了一小罐武夷岩茶；他笑说以后中期材料直接发三行摘要加 PDF 附件最省心。',
        snippet: '周二早十点前回信最快，正文前三行先列核心结论',
        stampCode: 'ACADEMY · 04.11',
      },
    ],
  },
  {
    slug: 'sumin',
    nameCn: '苏敏姐',
    monogram: '苏',
    roleTag: '合作制片',
    contextSub: '项目搭档 · 城西调色棚',
    tier: 'crystal',
    shortMemo: '审美利落守时，报价单需拆细项，晚十点后不发工作语音。',
    traits: [
      '对接档期习惯提前三天拉飞书表格，极重守时',
      '报价单需拆清器材与人工细项，忌笼统打包价',
      '偏爱浅烘手冲瑰夏，晚 22:00 后不打扰私人休息',
    ],
    visualTokens: {
      sealBg: 'linear-gradient(145deg, #eef5ef 0%, #d4e4d7 100%)',
      sealColor: '#2f5238',
      tapeColor: 'rgba(168, 196, 174, 0.78)',
      tabColor: '#7E9A86',
      folioDate: 'APR 14',
      hoboDay: '14',
      hoboMonthWk: 'APR · MON',
      ticketNo: 'NO. 0414-C',
      noteSign: '— 水晶箭手札 · 外部搭档',
      voiceDur: '0:10',
      stampCode: 'STUDIO · 04.14',
    },
    materials: [
      {
        kind: 'note',
        title: '苏敏姐 · 报价与作息边界卡',
        body: '春季短片看片会散场聊了十分钟，她推荐了城西靠谱的调色棚；下次审片记得带两杯浅烘瑰夏过去道谢。',
        snippet: '报价拆细项、提前三天定档期、晚十点后免打扰',
        stampCode: 'STUDIO · 04.14',
      },
    ],
  },
  {
    slug: 'laogu',
    nameCn: '老顾',
    monogram: '顾',
    roleTag: '公寓房东',
    contextSub: '街坊日常 · 梧桐公寓 502',
    tier: 'crystal',
    shortMemo: '退休木匠脾气温和，爱惜实木窗框，报修附清晰照片最省心。',
    traits: [
      '退休老木匠手艺讲究，沟通时站在爱惜房屋木作角度最顺畅',
      '月底水电抄表附清晰读数图，十月中旬提前聊续租',
      '血糖偏高忌甜腻，逢节送少糖绿豆糕最合心意',
    ],
    visualTokens: {
      sealBg: 'linear-gradient(145deg, #fbf0e6 0%, #ecd4be 100%)',
      sealColor: '#663c1f',
      tapeColor: 'rgba(222, 184, 156, 0.78)',
      tabColor: '#C9845B',
      folioDate: 'APR 06',
      hoboDay: '06',
      hoboMonthWk: 'APR · SUN',
      ticketNo: 'NO. 0406-C',
      noteSign: '— 水晶箭手札 · 屋檐日常',
      voiceDur: '0:09',
      stampCode: 'HOME · 04.06',
    },
    materials: [
      {
        kind: 'note',
        title: '老顾 · 房屋维护与少糖偏好',
        body: '老顾拎着木工箱过来调好了阳台纱窗滑轨，临走送了他两盒老字号少糖绿豆糕，老爷子乐呵呵说明年房租照旧。',
        snippet: '退休木匠爱惜木窗框，偏爱老字号少糖绿豆糕',
        stampCode: 'HOME · 04.06',
      },
    ],
  },
  {
    slug: 'xiaotang',
    nameCn: '小唐',
    monogram: '唐',
    roleTag: '摄影同好',
    contextSub: '银盐集市 · 胶片冲扫圈',
    tier: 'crystal',
    shortMemo: '熟知中古禄来行情，借了我一枚 49mm 柔焦镜下周擦净归还。',
    traits: [
      '熟知中古双反与银盐冲扫，纯粹聊摄影扫街最投缘',
      '4月13日借用他一枚 49mm 柔焦滤镜，归还前需蔡司纸擦净',
      '同好往来宜轻礼互赠（分装电影卷），不掺合彼此工作圈',
    ],
    visualTokens: {
      sealBg: 'linear-gradient(145deg, #ecf4f9 0%, #d0e2ee 100%)',
      sealColor: '#2b5069',
      tapeColor: 'rgba(166, 196, 218, 0.78)',
      tabColor: '#6E8CA8',
      folioDate: 'APR 13',
      hoboDay: '13',
      hoboMonthWk: 'APR · SUN',
      ticketNo: 'NO. 0413-C',
      noteSign: '— 水晶箭手札 · 兴趣同好',
      voiceDur: '0:08',
      stampCode: 'SILVER · 04.13',
    },
    materials: [
      {
        kind: 'note',
        title: '小唐 · 49mm 柔焦镜借还记录',
        body: '在银盐集市一起拼单了五卷 Portra 400，他顺手借了我一枚 49mm 柔焦滤镜试拍；下周归还时带一卷电影分装卷回礼。',
        snippet: '银盐集市拼单胶卷，借物准时擦净归还',
        stampCode: 'SILVER · 04.13',
      },
    ],
  },
  {
    slug: 'kevin',
    nameCn: 'Kevin',
    monogram: 'K',
    roleTag: '常驻发型师',
    contextSub: '生活常驻 · 周四午后清静档',
    tier: 'crystal',
    shortMemo: '固定“两侧留厚、刘海过眉半指”，周四下午三点人少零推销。',
    traits: [
      '固定修剪口令：两侧留厚不推白、刘海过眉半指微修层次',
      '每周四下午 15:00 店里最清静，提前两天微信预约即可',
      '家里养了一只刚绝育的橘猫，话少专注从不推销办卡',
    ],
    visualTokens: {
      sealBg: 'linear-gradient(145deg, #f3eef8 0%, #dfd4ec 100%)',
      sealColor: '#4c3863',
      tapeColor: 'rgba(196, 178, 214, 0.78)',
      tabColor: '#8E78A0',
      folioDate: 'MAR 29',
      hoboDay: '29',
      hoboMonthWk: 'MAR · SAT',
      ticketNo: 'NO. 0329-C',
      noteSign: '— 水晶箭手札 · 日常打理',
      voiceDur: '0:07',
      stampCode: 'SALON · 03.29',
    },
    materials: [
      {
        kind: 'note',
        title: 'Kevin · 专属修剪比例参数卡',
        body: '提前两天约周四下午三点店里最清静；坐下直接报“两侧留厚、刘海过眉半指”，四十分钟清爽剪完零废话。',
        snippet: '两侧留厚、刘海过眉半指 · 周四15:00最清静',
        stampCode: 'SALON · 03.29',
      },
    ],
  },
  {
    slug: 'aunt',
    nameCn: '林表姑',
    monogram: '姑',
    roleTag: '家族长辈',
    contextSub: '远房亲戚 · 逢节问候礼数',
    tier: 'crystal',
    shortMemo: '对海鲜严重过敏！逢节寄无蔗糖铁棍山药糕礼盒最稳妥。',
    traits: [
      '重要忌口：对海鲜严重过敏，节礼严禁含干贝/虾仁/海鲜粽',
      '热心爱聊养生茶与丝巾配色，顺着夸两句就开怀',
      '端午与中秋固定寄老字号无蔗糖铁棍山药糕即可',
    ],
    visualTokens: {
      sealBg: 'linear-gradient(145deg, #fbf0ee 0%, #edd5d1 100%)',
      sealColor: '#6e3834',
      tapeColor: 'rgba(228, 184, 176, 0.78)',
      tabColor: '#B86E68',
      folioDate: 'APR 04',
      hoboDay: '04',
      hoboMonthWk: 'APR · FRI',
      ticketNo: 'NO. 0404-C',
      noteSign: '— 水晶箭手札 · 家族礼数',
      voiceDur: '0:09',
      stampCode: 'FAMILY · 04.04',
    },
    materials: [
      {
        kind: 'note',
        title: '林表姑 · 海鲜过敏忌口警示卡',
        body: '清明家宴同桌表姑热心问起近况，夸她丝巾配色雅致就自然聊到了养生茶；切记她海鲜严重过敏，节礼选低糖山药糕。',
        snippet: '严禁任何海鲜干货成分，宜选无蔗糖山药糕',
        stampCode: 'FAMILY · 04.04',
      },
    ],
  },
];

/* ==========================================================================
   Session Storage & Authenticated Backend Client (`/v1/*`)
   ========================================================================== */
const STORAGE_SESSION_KEY = 'mw_prod_session_v1';
const STORAGE_GUEST_EMAIL_KEY = 'mw_prod_guest_email_v1';
const API_BASE = '/v1';

function makeUuid() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

function getOrCreateGuestEmail() {
  let email = localStorage.getItem(STORAGE_GUEST_EMAIL_KEY);
  if (!email) {
    const rand = Math.random().toString(16).slice(2, 10);
    email = `journal.${rand}@mindweave.app`;
    localStorage.setItem(STORAGE_GUEST_EMAIL_KEY, email);
  }
  return email;
}

function loadSavedSession() {
  try {
    const raw = localStorage.getItem(STORAGE_SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && parsed.accessToken && parsed.userId) return parsed;
  } catch (_) {}
  return null;
}

function saveSession(sess) {
  state.session = sess;
  if (sess) {
    localStorage.setItem(STORAGE_SESSION_KEY, JSON.stringify(sess));
  } else {
    localStorage.removeItem(STORAGE_SESSION_KEY);
  }
  syncAccountLabel();
}

let refreshPromise = null;

async function tryRefreshTokens() {
  if (!state.session || !state.session.refreshToken) return false;
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const resp = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refreshToken: state.session.refreshToken }),
      });
      if (!resp.ok) {
        saveSession(null);
        return false;
      }
      const data = await resp.json();
      saveSession({
        ...state.session,
        accessToken: data.accessToken,
        refreshToken: data.refreshToken,
      });
      return true;
    } catch (_) {
      return false;
    } finally {
      refreshPromise = null;
    }
  })();
  return refreshPromise;
}

async function apiFetch(path, options = {}, allowRetry = true) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  const isAuthEndpoint = path.startsWith('/auth/');

  if (!isAuthEndpoint && state.session && state.session.accessToken) {
    headers.set('Authorization', `Bearer ${state.session.accessToken}`);
  }
  if (['POST', 'PATCH', 'PUT', 'DELETE'].includes(method) && !isAuthEndpoint && !headers.has('Idempotency-Key')) {
    headers.set('Idempotency-Key', makeUuid());
  }
  if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData) && !(options.body instanceof Blob)) {
    headers.set('Content-Type', 'application/json');
  }

  const fetchInit = {
    ...options,
    method,
    headers,
    body:
      options.body && typeof options.body === 'object' && !(options.body instanceof FormData) && !(options.body instanceof Blob)
        ? JSON.stringify(options.body)
        : options.body,
  };

  const resp = await fetch(`${API_BASE}${path}`, fetchInit);
  if (resp.status === 401 && !isAuthEndpoint && allowRetry) {
    const refreshed = await tryRefreshTokens();
    if (refreshed) {
      return apiFetch(path, options, false);
    }
    switchScene('splash');
  }
  return resp;
}

async function apiJson(path, options = {}) {
  const resp = await apiFetch(path, options);
  if (resp.status === 204) return null;
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (_) {
    data = null;
  }
  if (!resp.ok) {
    const err = new Error((data && (data.detail || data.title)) || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.payload = data;
    throw err;
  }
  return data;
}

/* ==========================================================================
   Runtime State & Local People Dictionary
   ========================================================================== */
const PEOPLE_DB = {};

const state = {
  session: loadSavedSession(),
  otpChallenge: null,
  edition: 'cotton',
  cupidLayout: 'a',
  coreIds: [],
  casualIds: [],
  viewMode: 'person-journal',
  activePersonId: null,
  chatPersonId: null,
  lastDemotedId: null,
  activeScene: 'splash',
  capsuleExpanded: false,
  arrowPopoverOpen: false,
  drawerOpen: false,
  selectedMaterialIds: [],
  voiceHolding: false,
  voiceDragZone: 'current',
  stoneStateIndex: 2,
  newPersonTier: 'gold',
  deleteConfirmArmed: false,
  clearChatArmed: false,
  chatStreaming: false,
  lastDispatchSplits: [],
  lastCaptureUndo: null,
};

function preloadAllAssets() {
  [...Object.values(ARROW_PIN_IMAGES), ...CUPID_COMPANION_IMAGES].forEach((url) => {
    const img = new Image();
    img.src = url;
    if (img.decode) img.decode().catch(() => {});
  });
}

// Core DOM References
const viewport = document.getElementById('glebViewport');
const splashScreen = document.getElementById('splashScreen');
const splashMorphActor = document.getElementById('splashMorphActor');
const morphCapsuleGlass = document.getElementById('morphCapsuleGlass');

const topRightArrowAnchor = document.getElementById('topRightArrowAnchor');
const journalArrowPinBtn = document.getElementById('journalArrowPinBtn');
const journalArrowPinImg = document.getElementById('journalArrowPinImg');
const arrowSelectorPopover = document.getElementById('arrowSelectorPopover');
const btnBackToCasualList = document.getElementById('btnBackToCasualList');

const artifactPhotoMount = document.getElementById('artifactPhotoMount');
const artifactDossierCard = document.getElementById('artifactDossierCard');
const voiceExtractedMount = document.getElementById('voiceExtractedMount');
const casualEntriesStage = document.getElementById('casualEntriesStage');
const aiVoiceDispatchModal = document.getElementById('aiVoiceDispatchModal');
const btnUndoVoiceCapture = document.getElementById('btnUndoVoiceCapture');

const eventsFullScreen = document.getElementById('eventsFullScreen');
const aiChatFullscreen = document.getElementById('aiChatFullscreen');
const chatRightEdgeZone = document.getElementById('chatRightEdgeZone');
const chatMaterialScrim = document.getElementById('chatMaterialScrim');
const chatMaterialDrawer = document.getElementById('chatMaterialDrawer');
const drawerMaterialsList = document.getElementById('drawerMaterialsList');
const drawerSelectedCount = document.getElementById('drawerSelectedCount');
const chatAttachedTray = document.getElementById('chatAttachedTray');
const chatTextInput = document.getElementById('chatTextInput');

const glebTriDock = document.getElementById('glebTriDock');
const dockVoiceOrbBtn = document.getElementById('dockVoiceOrbBtn');
const dockCenterCapsule = document.getElementById('dockCenterCapsule');
const capsulePortraitCluster = document.getElementById('capsulePortraitCluster');
const capsuleExpandedItems = document.getElementById('capsuleExpandedItems');

const voiceHoldStage = document.getElementById('voiceHoldStage');
const voiceHoldTimerEl = document.getElementById('voiceHoldTimer');
const voiceSilkCanvas = document.getElementById('voiceSilkCanvas');
const voiceSilkCtx = voiceSilkCanvas ? voiceSilkCanvas.getContext('2d') : null;
const voiceDragPuck = document.getElementById('voiceDragPuck');

const moodStonePopover = document.getElementById('moodStonePopover');
const stoneCanvas = document.getElementById('stoneGrowthCanvas');
const stoneCtx = stoneCanvas ? stoneCanvas.getContext('2d') : null;
const stoneSliderWrap = document.getElementById('stoneSliderWrap');
const stoneSliderKnob = document.getElementById('stoneSliderKnob');
const sliderDots = Array.from(document.querySelectorAll('#stoneSliderDots .stone-slider-dot'));

const personCreateModal = document.getElementById('personCreateModal');
const noteEditModal = document.getElementById('noteEditModal');
const quickCaptureSheet = document.getElementById('quickCaptureSheet');
const photoUploadInput = document.getElementById('photoUploadInput');

function clamp01(v) {
  return Math.max(0, Math.min(1, v));
}
function easeOutCubic(t) {
  return 1 - Math.pow(1 - t, 3);
}
function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function formatTodayTokens() {
  const now = new Date();
  const months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
  const wks = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
  const m = months[now.getMonth()];
  const d = String(now.getDate()).padStart(2, '0');
  return {
    folioDate: `${m} ${d}`,
    hoboDay: d,
    hoboMonthWk: `${m} · ${wks[now.getDay()]}`,
    stampCode: `JOURNAL · ${String(now.getMonth() + 1).padStart(2, '0')}.${d}`,
    ticketNo: `NO. ${String(now.getMonth() + 1).padStart(2, '0')}${d}`,
  };
}

function syncAccountLabel() {
  const el = document.getElementById('stoneAccountLabel');
  if (!el) return;
  if (!state.session || !state.session.email) {
    el.textContent = '心络手帐';
    return;
  }
  const mail = state.session.email;
  el.textContent = mail.endsWith('@mindweave.app') ? '本机手帐' : mail;
}

/* ==========================================================================
   Data Hydration from `GET /v1/bootstrap`
   ========================================================================== */
function hydratePersonFromDto(dto) {
  const vt = dto.visualTokens || {};
  const today = formatTodayTokens();
  const materials = Array.isArray(dto.materials) ? dto.materials : [];

  const mainPhoto = dto.mainPhotoUrl || dto.mainPhoto || vt.fallbackMainPhoto || '';
  const subPhoto = dto.subPhotoUrl || dto.subPhoto || vt.fallbackSubPhoto || mainPhoto || './assets/photos/photo_12.jpg';
  const avatar = dto.avatarUrl || dto.avatar || vt.fallbackAvatar || mainPhoto || '';
  const hasPhoto = Boolean(dto.hasPhoto || mainPhoto);

  const noteMats = materials.filter((m) => m.type === 'note' || m.kind === 'note');
  const voiceMats = materials.filter((m) => m.type === 'voice' || m.kind === 'voice');
  const primaryNote = noteMats[0] || materials[0] || null;

  const voiceSlips = voiceMats.map((m) => ({
    id: String(m.id),
    tag: m.title || '原声手札',
    dur: m.dur || '0:06',
    body: m.body || m.snippet || '',
  }));

  const normalizedMats = materials.map((m) => {
    const kind = m.type || m.kind || 'note';
    return {
      id: String(m.id),
      type: kind,
      thumb: m.thumb || m.mediaUrl || mainPhoto || './assets/photos/photo_9.jpg',
      icon: kind === 'voice' ? '♪' : '✎',
      title: m.title || '手帐便签',
      body: m.body || m.snippet || '',
      snippet: m.snippet || m.body || '',
    };
  });

  const noteBody =
    (primaryNote && primaryNote.body) ||
    dto.shortMemo ||
    `关于${dto.nameCn}的点滴细节，都安静收在这一页里。`;

  const traits =
    Array.isArray(dto.traits) && dto.traits.length > 0
      ? dto.traits
      : dto.shortMemo
      ? [dto.shortMemo]
      : [noteBody];

  return {
    id: String(dto.id),
    nameCn: dto.nameCn,
    monogram: dto.monogram || dto.nameCn.slice(0, 1),
    roleTag: dto.roleTag || '日常往来',
    contextSub: dto.contextSub || '心络手帐',
    sealBg: dto.sealBg || vt.sealBg || 'linear-gradient(145deg, #fcf4e6 0%, #efd9b8 100%)',
    sealColor: dto.sealColor || vt.sealColor || '#694418',
    tapeColor: dto.tapeColor || vt.tapeColor || 'rgba(226, 182, 118, 0.76)',
    tabColor: dto.tabColor || vt.tabColor || '#D49A45',
    cupidTitle: dto.cupidTitle || `${dto.nameCn}的丘比特`,
    tier: VALID_TIERS.includes(dto.tier) ? dto.tier : 'crystal',
    hasPhoto,
    avatar,
    mainPhoto: mainPhoto || './assets/photos/photo_9.jpg',
    subPhoto,
    folioDate: vt.folioDate || today.folioDate,
    hoboDay: vt.hoboDay || today.hoboDay,
    hoboMonthWk: vt.hoboMonthWk || today.hoboMonthWk,
    ticketNo: vt.ticketNo || today.ticketNo,
    captionCotton: vt.captionCotton || `${dto.nameCn} · ${dto.contextSub || '日常留痕'}`,
    captionFilm: vt.captionFilm || `FILM ARCHIVE · ${vt.folioDate || today.folioDate}`,
    shortMemo: dto.shortMemo || (primaryNote ? primaryNote.snippet : '') || '轻触手札记录日常偏好与分寸。',
    traits,
    noteBody,
    primaryNoteId: primaryNote ? String(primaryNote.id) : null,
    noteSign: vt.noteSign || `— ${dto.nameCn} · 手札`,
    voiceDur: (voiceMats[0] && voiceMats[0].dur) || vt.voiceDur || '0:12',
    stampCode: vt.stampCode || today.stampCode,
    visualTokens: vt,
    voiceSlips,
    materials: normalizedMats,
    version: dto.version || 1,
  };
}

async function seedInitialJournalIfEmpty() {
  if (!state.session || !state.session.userId) return;
  const seedFlagKey = `mw_seeded_v1_${state.session.userId}`;
  if (localStorage.getItem(seedFlagKey)) return;

  for (const tpl of INITIAL_SEED_PROFILES) {
    const created = await apiJson('/persons', {
      method: 'POST',
      body: {
        slug: tpl.slug,
        nameCn: tpl.nameCn,
        monogram: tpl.monogram,
        roleTag: tpl.roleTag,
        contextSub: tpl.contextSub,
        shortMemo: tpl.shortMemo,
        traits: tpl.traits,
        tier: tpl.tier,
        visualTokens: tpl.visualTokens,
      },
    });
    if (created && created.id && Array.isArray(tpl.materials)) {
      await Promise.all(
        tpl.materials.map((mat) =>
          apiJson(`/persons/${created.id}/materials`, {
            method: 'POST',
            body: {
              kind: mat.kind,
              title: mat.title,
              body: mat.body,
              snippet: mat.snippet,
              stampCode: mat.stampCode,
              pinned: true,
            },
          })
        )
      );
    }
  }
  localStorage.setItem(seedFlagKey, '1');
}

async function refreshBootstrap({ allowSeed = false } = {}) {
  let snap = await apiJson('/bootstrap');
  if (
    allowSeed &&
    snap &&
    (!snap.people || Object.keys(snap.people).length === 0)
  ) {
    await seedInitialJournalIfEmpty();
    snap = await apiJson('/bootstrap');
  }

  Object.keys(PEOPLE_DB).forEach((k) => delete PEOPLE_DB[k]);
  const rawPeople = (snap && snap.people) || {};
  Object.entries(rawPeople).forEach(([id, dto]) => {
    PEOPLE_DB[id] = hydratePersonFromDto(dto);
  });

  state.coreIds = (snap.coreIds || []).map(String).filter((id) => Boolean(PEOPLE_DB[id]));
  state.casualIds = (snap.casualIds || []).map(String).filter((id) => Boolean(PEOPLE_DB[id]));

  if (snap.preferences) {
    const ed = snap.preferences.preferredEdition || snap.preferences.edition;
    if (VALID_EDITIONS.includes(ed)) {
      setJournalEdition(ed, false);
    }
    const cl = snap.preferences.cupidLayout;
    if (VALID_CUPID_LAYOUTS.includes(cl)) {
      setCupidLayout(cl, false);
    }
  }

  if (!state.activePersonId || !PEOPLE_DB[state.activePersonId]) {
    state.activePersonId = state.coreIds[0] || state.casualIds[0] || null;
  }

  if (state.viewMode === 'casual-directory') {
    openCasualDirectory();
  } else if (state.activePersonId && PEOPLE_DB[state.activePersonId]) {
    openPersonJournal(state.activePersonId);
  } else {
    openCasualDirectory();
  }
  return snap;
}

/* ==========================================================================
   1. 31-Frame Botanical Mood Stone Mini-Engine
   ========================================================================== */
const STRIP_FRAME_COUNT = 31;
const STRIP_FRAME_W = 220;
const STRIP_FRAME_H = 200;
const stoneStripImg = new Image();
let stoneStripLoaded = false;
let currentStoneFrame = 16;

function drawStoneFrame(frameFloat) {
  if (!stoneCtx || !stoneStripLoaded) return;
  const f = Math.max(0, Math.min(STRIP_FRAME_COUNT - 1, Math.round(frameFloat)));
  stoneCtx.clearRect(0, 0, STRIP_FRAME_W, STRIP_FRAME_H);
  stoneCtx.drawImage(
    stoneStripImg,
    f * STRIP_FRAME_W,
    0,
    STRIP_FRAME_W,
    STRIP_FRAME_H,
    0,
    0,
    STRIP_FRAME_W,
    STRIP_FRAME_H
  );
}
stoneStripImg.onload = () => {
  stoneStripLoaded = true;
  drawStoneFrame(currentStoneFrame);
};
stoneStripImg.src = './assets/gleb/stone_growth_strip.webp';

function setStoneByPercent(pct) {
  const clamped = Math.max(8, Math.min(92, pct));
  const idx = clamped < 26 ? 0 : clamped < 51 ? 1 : clamped < 77 ? 2 : 3;
  state.stoneStateIndex = idx;
  const spec = STONE_STATES[idx];
  currentStoneFrame = spec.frameIndex;
  drawStoneFrame(currentStoneFrame);
  if (stoneSliderKnob) stoneSliderKnob.style.left = `${clamped}%`;
  const wordEl = document.getElementById('stoneMoodWord');
  if (wordEl) wordEl.textContent = spec.word;
  sliderDots.forEach((d, i) => d.classList.toggle('is-active', i === spec.dotIndex));
}

/* ==========================================================================
   2. Dynamic Capsule Slots & Casual Directory Rendering
   ========================================================================== */
function renderAvatarInnerHtml(person) {
  if (person && person.hasPhoto && person.avatar) {
    return `<img src="${escapeHtml(person.avatar)}" alt="${escapeHtml(person.nameCn)}" draggable="false" />`;
  }
  return `<span>${escapeHtml(person ? person.monogram : '心')}</span>`;
}

export function renderNavigationAndCapsule() {
  if (capsuleExpandedItems && dockCenterCapsule) {
    const totalSlots = state.coreIds.length + 2;
    const expandedW = Math.min(346, Math.max(156, totalSlots * 48 + 20));
    dockCenterCapsule.style.setProperty('--expanded-capsule-w', `${expandedW}px`);

    const coreAvatarsHtml = state.coreIds
      .map((id) => {
        const p = PEOPLE_DB[id];
        if (!p) return '';
        const isSelected = state.viewMode === 'person-journal' && state.activePersonId === id;
        return `
          <button
            type="button"
            class="capsule-obj-avatar ${isSelected ? 'is-selected' : ''}"
            data-capsule-target="${escapeHtml(id)}"
            data-tier="${escapeHtml(p.tier)}"
            aria-label="${escapeHtml(p.nameCn)}"
          >
            ${renderAvatarInnerHtml(p)}
          </button>
        `;
      })
      .join('');

    const isCasualMode =
      state.viewMode === 'casual-directory' || state.casualIds.includes(state.activePersonId);

    const casualSlotHtml = `
      <button
        type="button"
        class="capsule-obj-avatar is-casual-icon ${isCasualMode ? 'is-selected' : ''}"
        data-capsule-target="__casual_dir__"
        data-tier="crystal"
        aria-label="记繁琐"
      >
        <span class="casual-token-inner">${CASUAL_DIRECTORY_SVG}</span>
      </button>
    `;

    const addSlotHtml = `
      <button
        type="button"
        class="capsule-obj-avatar is-add-person"
        data-capsule-target="__add_person__"
        aria-label="新添人物"
      >＋</button>
    `;

    capsuleExpandedItems.innerHTML = coreAvatarsHtml + casualSlotHtml + addSlotHtml;
  }

  if (capsulePortraitCluster) {
    const isCasualMode =
      state.viewMode === 'casual-directory' || state.casualIds.includes(state.activePersonId);

    capsulePortraitCluster.classList.toggle('is-casual-mode', isCasualMode);

    if (isCasualMode) {
      const corePortraitsHtml = state.coreIds
        .slice(0, 3)
        .map((id, idx) => {
          const p = PEOPLE_DB[id];
          return p
            ? `<span class="cluster-mini-avatar core-slot-${idx}" aria-hidden="true">${renderAvatarInnerHtml(p)}</span>`
            : '';
        })
        .join('');

      capsulePortraitCluster.innerHTML = `
        ${corePortraitsHtml}
        <span class="cluster-mini-casual is-active-casual" aria-hidden="true">${CASUAL_DIRECTORY_SVG}</span>
      `;
    } else {
      const cur =
        PEOPLE_DB[state.activePersonId] ||
        PEOPLE_DB[state.coreIds[0]] ||
        PEOPLE_DB[state.casualIds[0]] ||
        null;
      const others = cur ? state.coreIds.filter((id) => id !== cur.id) : [];
      const leftP = others[0] ? PEOPLE_DB[others[0]] : null;
      const rightP = others[1] ? PEOPLE_DB[others[1]] : null;

      capsulePortraitCluster.innerHTML = `
        ${leftP ? `<span class="cluster-mini-avatar pos-left" aria-hidden="true">${renderAvatarInnerHtml(leftP)}</span>` : ''}
        <div class="capsule-active-token">${renderAvatarInnerHtml(cur)}</div>
        ${rightP ? `<span class="cluster-mini-avatar pos-right" aria-hidden="true">${renderAvatarInnerHtml(rightP)}</span>` : ''}
        <span class="cluster-mini-casual" aria-hidden="true">${CASUAL_DIRECTORY_SVG}</span>
      `;
    }
  }

  renderCasualDirectory();
}

export function renderCasualDirectory() {
  const countEl = document.getElementById('casualDirectoryCount');
  if (countEl) countEl.textContent = `${state.casualIds.length}`;
  if (!casualEntriesStage) return;

  casualEntriesStage.innerHTML = state.casualIds
    .map((id) => {
      const p = PEOPLE_DB[id];
      if (!p) return '';
      const isJustDemoted = state.lastDemotedId === id;
      const hasFreshVoiceSlip = p.voiceSlips && p.voiceSlips.length > 0;
      const rightMetaHtml = hasFreshVoiceSlip
        ? `<span class="folio-fresh-dot">●</span>`
        : `<span class="folio-date-whisper">${escapeHtml(p.folioDate)}</span>`;

      return `
        <button
          type="button"
          class="casual-folio-row ${isJustDemoted ? 'is-just-demoted' : ''}"
          data-open-casual-person="${escapeHtml(p.id)}"
          role="listitem"
        >
          <span class="folio-side-tab" style="background:${escapeHtml(p.tabColor)};" aria-hidden="true"></span>
          <span class="folio-monogram-seal" style="background:${escapeHtml(p.sealBg)}; color:${escapeHtml(p.sealColor)};" aria-hidden="true">
            ${escapeHtml(p.monogram)}
          </span>
          <div class="folio-body">
            <div class="folio-top-line">
              <span class="folio-name">${escapeHtml(p.nameCn)}</span>
              <span class="folio-badge">${escapeHtml(p.roleTag)}</span>
              ${rightMetaHtml}
            </div>
            <p class="folio-memo">${escapeHtml(p.shortMemo)}</p>
          </div>
          <span class="folio-chevron" aria-hidden="true">›</span>
        </button>
      `;
    })
    .join('');
}

/* ==========================================================================
   3. Scrapbook Visual Editions, Tactile Arrow Pin & Real Tier Transitions
   ========================================================================== */
export function setJournalEdition(editionName, persist = true) {
  const valid = VALID_EDITIONS.includes(editionName) ? editionName : 'cotton';
  state.edition = valid;
  viewport.setAttribute('data-journal-edition', valid);

  const p = PEOPLE_DB[state.activePersonId];
  const captionEl = document.getElementById('journalPhotoCaption');
  if (captionEl && p && p.hasPhoto) {
    captionEl.textContent = valid === 'dotgrid' ? p.captionFilm : p.captionCotton;
  }

  if (persist && state.session) {
    apiJson('/me/preferences', {
      method: 'PATCH',
      body: { preferredEdition: valid },
    }).catch(() => {});
  }
}

function getEffectiveTier() {
  if (state.viewMode === 'casual-directory') return 'crystal';
  const p = PEOPLE_DB[state.activePersonId];
  return p && VALID_TIERS.includes(p.tier) ? p.tier : 'gold';
}

function syncArrowPinAndChatCupid(animateStrike = false) {
  const tier = getEffectiveTier();

  viewport.setAttribute('data-arrow-tier', tier);
  aiChatFullscreen.setAttribute('data-arrow-tier', tier);
  aiChatFullscreen.setAttribute('data-cupid-layout', state.cupidLayout);

  if (journalArrowPinBtn) {
    journalArrowPinBtn.setAttribute('data-tier', tier);
    if (animateStrike) {
      journalArrowPinBtn.classList.remove('is-striking');
      void journalArrowPinBtn.offsetWidth;
      journalArrowPinBtn.classList.add('is-striking');
    }
  }
  if (journalArrowPinImg) {
    journalArrowPinImg.src = ARROW_PIN_IMAGES[tier] || ARROW_PIN_IMAGES.gold;
  }

  const chatHeaderArrow = document.getElementById('chatHeaderArrowWrap');
  if (chatHeaderArrow) chatHeaderArrow.innerHTML = ARROW_SVGS[tier] || ARROW_SVGS.gold;

  const portraitImg = document.getElementById('museCupidPortraitImg');
  if (portraitImg) {
    const nextSrc = MUSE_CUPID_PORTRAITS[state.cupidLayout] || MUSE_CUPID_PORTRAITS.a;
    if (portraitImg.getAttribute('src') !== nextSrc) {
      portraitImg.src = nextSrc;
    }
  }

  document.querySelectorAll('[data-select-tier]').forEach((item) => {
    const selected = item.dataset.selectTier === tier;
    item.classList.toggle('is-selected', selected);
    item.setAttribute('aria-checked', String(selected));
  });
}

export function triggerCupidCompanionGreet() {
  const medallion = document.getElementById('museCupidMedallion');
  if (!medallion) return;
  medallion.classList.remove('is-cupid-greeting');
  void medallion.offsetWidth;
  medallion.classList.add('is-cupid-greeting');
  setTimeout(() => medallion.classList.remove('is-cupid-greeting'), 580);
}

export function setCupidLayout(layoutName, persist = true) {
  const valid = VALID_CUPID_LAYOUTS.includes(layoutName) ? layoutName : 'a';
  state.cupidLayout = valid;
  syncArrowPinAndChatCupid(false);
  triggerCupidCompanionGreet();
  if (persist && state.session) {
    apiJson('/me/preferences', {
      method: 'PATCH',
      body: { cupidLayout: valid },
    }).catch(() => {});
  }
}

export function setArrowPopoverOpen(open) {
  state.arrowPopoverOpen = Boolean(open);
  arrowSelectorPopover.classList.toggle('is-open', state.arrowPopoverOpen);
  arrowSelectorPopover.setAttribute('aria-hidden', String(!state.arrowPopoverOpen));
  journalArrowPinBtn.setAttribute('aria-expanded', String(state.arrowPopoverOpen));
}

function renderPersonVoiceSlips(person) {
  const hasSlips = Boolean(person && Array.isArray(person.voiceSlips) && person.voiceSlips.length > 0);
  if (viewport) viewport.setAttribute('data-has-voice-slips', String(hasSlips));
  if (!voiceExtractedMount) return;
  if (!hasSlips) {
    voiceExtractedMount.innerHTML = '';
    return;
  }
  const recentSlips = person.voiceSlips.slice(0, 2);
  voiceExtractedMount.innerHTML = recentSlips
    .map(
      (s) => `
      <article class="voice-extracted-slip" data-slip-tier="${escapeHtml(person.tier)}">
        <span class="slip-washi-strip" aria-hidden="true"></span>
        <div class="slip-top-meta">
          <span class="slip-ai-tag">✦ ${escapeHtml(s.tag)}</span>
          <span class="slip-voice-stamp">♪ ${escapeHtml(s.dur)}</span>
        </div>
        <p class="slip-event-body">${escapeHtml(s.body)}</p>
      </article>
    `
    )
    .join('');
}

function syncEventsViewForPerson(p) {
  if (!p) return;
  const c1Photo = document.getElementById('eventsCard1Photo');
  const c1Loc = document.getElementById('eventsCard1Loc');
  const c1Owner = document.getElementById('eventsCard1Owner');
  const c1Back = document.getElementById('eventsCard1BackText');
  const c1Dur = document.getElementById('eventsCard1Dur');
  const av1 = document.getElementById('eventsAvatar1');
  const c2Back = document.getElementById('eventsCard2BackText');
  const c2Loc = document.getElementById('eventsCard2Loc');

  if (c1Photo && p.hasPhoto && p.mainPhoto) c1Photo.src = p.mainPhoto;
  if (av1 && p.avatar) av1.src = p.avatar;
  if (c1Loc) c1Loc.textContent = `${p.folioDate} · ${p.contextSub} · ${p.nameCn}`;
  if (c1Owner) c1Owner.textContent = p.nameCn;
  if (c1Back) c1Back.textContent = `“${p.noteBody}”`;
  if (c1Dur) c1Dur.textContent = `▶ ${p.voiceDur || '0:18'}`;
  if (c2Loc) c2Loc.textContent = `${p.folioDate} · ${p.roleTag}`;
  if (c2Back) c2Back.textContent = `“${p.shortMemo}”`;
}

export function openPersonJournal(personId) {
  const p = PEOPLE_DB[personId] || PEOPLE_DB[state.coreIds[0]] || PEOPLE_DB[state.casualIds[0]];
  if (!p) {
    openCasualDirectory();
    return;
  }
  state.viewMode = 'person-journal';
  state.activePersonId = p.id;

  setArrowPopoverOpen(false);
  viewport.setAttribute('data-view-mode', 'person-journal');
  viewport.setAttribute('data-has-photo', String(Boolean(p.hasPhoto)));

  const isCasualPerson = state.casualIds.includes(p.id);
  if (btnBackToCasualList) {
    btnBackToCasualList.hidden = !isCasualPerson;
  }

  document.getElementById('journalFolioDate').textContent = p.folioDate;
  document.getElementById('journalFolioSubject').textContent = p.nameCn;
  const hoboDayEl = document.getElementById('hoboBigDay');
  const hoboMonthEl = document.getElementById('hoboMonthWk');
  const ticketNoEl = document.getElementById('ticketSerialNo');
  if (hoboDayEl) hoboDayEl.textContent = p.hoboDay;
  if (hoboMonthEl) hoboMonthEl.textContent = p.hoboMonthWk;
  if (ticketNoEl) ticketNoEl.textContent = p.ticketNo;

  if (p.hasPhoto) {
    artifactPhotoMount.hidden = false;
    artifactDossierCard.hidden = true;
    document.getElementById('journalMainPhoto').src = p.mainPhoto;
    document.getElementById('journalSubPhoto').src = p.subPhoto;
    document.getElementById('journalPhotoCaption').textContent =
      state.edition === 'dotgrid' ? p.captionFilm : p.captionCotton;
  } else {
    artifactPhotoMount.hidden = true;
    artifactDossierCard.hidden = false;
    document.getElementById('dossierTopTape').style.background = p.tapeColor;
    const monoEl = document.getElementById('dossierMonogram');
    monoEl.textContent = p.monogram;
    monoEl.style.background = p.sealBg;
    monoEl.style.color = p.sealColor;
    document.getElementById('dossierPersonName').textContent = p.nameCn;
    document.getElementById('dossierRoleTag').textContent = p.roleTag;
    document.getElementById('dossierContextSub').textContent = p.contextSub;
    document.getElementById('dossierTraitsList').innerHTML = (p.traits || [])
      .map((t) => `<li>${escapeHtml(t)}</li>`)
      .join('');
  }

  document.getElementById('journalNoteBody').textContent = p.noteBody;
  document.getElementById('journalNoteSign').textContent = p.noteSign;
  document.getElementById('journalVoiceDur').textContent = p.voiceDur;
  document.getElementById('journalStampCode').textContent = p.stampCode;
  renderPersonVoiceSlips(p);
  syncEventsViewForPerson(p);

  syncArrowPinAndChatCupid(false);
  renderNavigationAndCapsule();
}

export function openCasualDirectory() {
  state.viewMode = 'casual-directory';
  setArrowPopoverOpen(false);
  viewport.setAttribute('data-view-mode', 'casual-directory');

  if (btnBackToCasualList) btnBackToCasualList.hidden = true;
  document.getElementById('journalFolioDate').textContent = 'CRYSTAL';
  document.getElementById('journalFolioSubject').textContent = '记繁琐名录';

  syncArrowPinAndChatCupid(false);
  renderNavigationAndCapsule();
}

export async function changeActivePersonArrowTier(newTier) {
  const normalized = newTier === 'none' ? 'crystal' : newTier;
  if (!VALID_TIERS.includes(normalized)) return;
  if (state.viewMode !== 'person-journal') return;

  const person = PEOPLE_DB[state.activePersonId];
  if (!person) return;

  const prevTier = person.tier;
  if (prevTier === normalized) {
    setArrowPopoverOpen(false);
    return;
  }

  person.tier = normalized;
  setArrowPopoverOpen(false);

  const wasInCore = state.coreIds.includes(person.id);

  if (normalized === 'crystal' && wasInCore) {
    state.coreIds = state.coreIds.filter((id) => id !== person.id);
    state.casualIds = [person.id, ...state.casualIds.filter((id) => id !== person.id)];
    state.lastDemotedId = person.id;
    openCasualDirectory();
  } else if ((normalized === 'gold' || normalized === 'silver') && !wasInCore) {
    state.casualIds = state.casualIds.filter((id) => id !== person.id);
    if (!state.coreIds.includes(person.id)) {
      state.coreIds.push(person.id);
    }
    if (state.lastDemotedId === person.id) state.lastDemotedId = null;
    openPersonJournal(person.id);
    syncArrowPinAndChatCupid(true);
  } else {
    syncArrowPinAndChatCupid(true);
    renderNavigationAndCapsule();
  }

  try {
    const res = await apiJson(`/persons/${person.id}/tier`, {
      method: 'PATCH',
      body: { tier: normalized },
    });
    if (res && Array.isArray(res.coreIds) && Array.isArray(res.casualIds)) {
      state.coreIds = res.coreIds.map(String).filter((id) => Boolean(PEOPLE_DB[id]));
      state.casualIds = res.casualIds.map(String).filter((id) => Boolean(PEOPLE_DB[id]));
      renderNavigationAndCapsule();
    }
  } catch (_) {
    await refreshBootstrap();
  }
}

/* ==========================================================================
   4. Splash -> Center Capsule Morph & Seamless Real Authentication
   ========================================================================== */
let morphRafId = null;
let emergeTimerId = null;

export function renderSplashMorphAtProgress(rawP) {
  if (!viewport || !splashMorphActor || !dockCenterCapsule) return;
  const p = clamp01(rawP);
  const vpRect = viewport.getBoundingClientRect();
  const capRect = dockCenterCapsule.getBoundingClientRect();

  const localVw = viewport.offsetWidth || 384;
  const localVh = viewport.offsetHeight || 838;
  const invScale = vpRect.width > 0 ? localVw / vpRect.width : 1;

  const targetW = capRect.width > 0 ? capRect.width * invScale : 146;
  const targetH = capRect.height > 0 ? capRect.height * invScale : 54;
  const targetLeft = capRect.width > 0 ? (capRect.left - vpRect.left) * invScale : (localVw - targetW) * 0.5;
  const targetTop = capRect.height > 0 ? (capRect.top - vpRect.top) * invScale : localVh - 18 - targetH;

  const kW = easeOutCubic(clamp01(p / 0.84));
  const kH = 1 - Math.pow(1 - clamp01(p / 0.94), 2.35);

  const w = localVw + (targetW - localVw) * kW;
  const h = localVh + (targetH - localVh) * kH;
  const left = (localVw - w) * 0.5 + (targetLeft - (localVw - targetW) * 0.5) * kW;

  const kTop = easeOutCubic(clamp01(p / 0.9));
  const springDip = p > 0.82 ? Math.sin(((p - 0.82) / 0.18) * Math.PI) * 2.8 : 0;
  const top = targetTop * kTop + springDip;
  const radius = 46 + (targetH * 0.5 - 46) * easeOutCubic(clamp01(p / 0.76));

  splashMorphActor.classList.add('is-morphing');
  splashMorphActor.style.left = `${left.toFixed(2)}px`;
  splashMorphActor.style.top = `${top.toFixed(2)}px`;
  splashMorphActor.style.width = `${w.toFixed(2)}px`;
  splashMorphActor.style.height = `${h.toFixed(2)}px`;
  splashMorphActor.style.borderRadius = `${radius.toFixed(2)}px`;

  if (morphCapsuleGlass) {
    morphCapsuleGlass.style.opacity = clamp01((p - 0.58) / 0.36).toFixed(3);
  }
}

export function playSplashToCapsuleMorph() {
  if (morphRafId) cancelAnimationFrame(morphRafId);
  if (emergeTimerId) clearTimeout(emergeTimerId);

  setCapsuleExpanded(false);
  setArrowPopoverOpen(false);
  setMaterialDrawerOpen(false);
  closeDispatchModal();
  eventsFullScreen.classList.remove('is-active');
  aiChatFullscreen.classList.remove('is-active');
  cancelVoiceHoldSession();
  glebTriDock.style.display = 'flex';

  dockCenterCapsule.classList.add('is-pure-landscape');
  state.activeScene = 'journal';

  splashScreen.classList.add('is-fading-copy');

  setTimeout(() => {
    renderSplashMorphAtProgress(0);
    splashScreen.classList.add('is-instant-hide');
    splashScreen.classList.remove('is-active', 'is-fading-copy');
    requestAnimationFrame(() => splashScreen.classList.remove('is-instant-hide'));

    const durationMs = 700;
    const startTs = performance.now();

    const tick = (now) => {
      const p = clamp01((now - startTs) / durationMs);
      renderSplashMorphAtProgress(p);
      if (p < 1) {
        morphRafId = requestAnimationFrame(tick);
      } else {
        morphRafId = null;
        splashMorphActor.classList.remove('is-morphing');
        emergeTimerId = setTimeout(() => {
          requestAnimationFrame(() => dockCenterCapsule.classList.remove('is-pure-landscape'));
        }, 180);
      }
    };
    morphRafId = requestAnimationFrame(tick);
  }, 105);
}

async function ensureAuthenticatedAndBootJournal() {
  const btn = document.getElementById('btnSplashLetsGo');
  const label = document.getElementById('splashLetsGoLabel');
  if (btn) btn.disabled = true;
  if (label) label.textContent = '开启中…';

  try {
    if (!state.session || !state.session.accessToken) {
      const guestEmail = getOrCreateGuestEmail();
      const ch = await apiJson('/auth/email/otp:request', {
        method: 'POST',
        body: { email: guestEmail, purpose: 'login' },
      });
      if (!ch || !ch.devOtpCode) {
        toggleSplashEmailForm(true);
        if (label) label.textContent = "Let's Go";
        if (btn) btn.disabled = false;
        return;
      }
      const verified = await apiJson('/auth/email/otp:verify', {
        method: 'POST',
        body: {
          challengeId: ch.challengeId,
          email: guestEmail,
          code: ch.devOtpCode,
        },
      });
      saveSession({
        userId: verified.userId,
        email: guestEmail,
        accessToken: verified.accessToken,
        refreshToken: verified.refreshToken,
      });
    }

    await refreshBootstrap({ allowSeed: true });
    playSplashToCapsuleMorph();
  } catch (_) {
    saveSession(null);
    toggleSplashEmailForm(true);
  } finally {
    if (label) label.textContent = "Let's Go";
    if (btn) btn.disabled = false;
  }
}

function toggleSplashEmailForm(open) {
  const primaryActions = document.getElementById('splashPrimaryActions');
  const form = document.getElementById('splashAuthForm');
  const otpRow = document.getElementById('splashOtpRow');
  const hint = document.getElementById('splashAuthHint');
  if (primaryActions) primaryActions.hidden = Boolean(open);
  if (form) form.hidden = !open;
  if (!open) {
    if (otpRow) otpRow.hidden = true;
    if (hint) hint.textContent = '';
    state.otpChallenge = null;
  } else {
    const emailInput = document.getElementById('splashEmailInput');
    if (emailInput) emailInput.focus();
  }
}

async function handleSendEmailOtp() {
  const emailInput = document.getElementById('splashEmailInput');
  const otpRow = document.getElementById('splashOtpRow');
  const otpInput = document.getElementById('splashOtpInput');
  const hint = document.getElementById('splashAuthHint');
  const btnSend = document.getElementById('btnSendOtp');

  const email = (emailInput ? emailInput.value : '').trim().toLowerCase();
  if (!email || !email.includes('@')) {
    if (hint) hint.textContent = '请输入有效邮箱';
    return;
  }

  if (btnSend) btnSend.disabled = true;
  if (hint) hint.textContent = '发送中…';

  try {
    const ch = await apiJson('/auth/email/otp:request', {
      method: 'POST',
      body: { email, purpose: 'login' },
    });
    state.otpChallenge = { challengeId: ch.challengeId, email };
    if (otpRow) otpRow.hidden = false;
    if (ch.devOtpCode && otpInput) {
      otpInput.value = ch.devOtpCode;
      if (hint) hint.textContent = '验证码已就绪';
    } else if (hint) {
      hint.textContent = '请查收邮箱验证码';
    }
    if (otpInput) otpInput.focus();
  } catch (_) {
    if (hint) hint.textContent = '发送稍忙，请重试';
  } finally {
    if (btnSend) btnSend.disabled = false;
  }
}

async function handleVerifyEmailOtp(e) {
  e.preventDefault();
  const emailInput = document.getElementById('splashEmailInput');
  const otpInput = document.getElementById('splashOtpInput');
  const hint = document.getElementById('splashAuthHint');
  const btnVerify = document.getElementById('btnVerifyOtp');

  const email = (state.otpChallenge && state.otpChallenge.email) || (emailInput ? emailInput.value.trim().toLowerCase() : '');
  const code = otpInput ? otpInput.value.trim() : '';
  if (!state.otpChallenge || !code) {
    await handleSendEmailOtp();
    return;
  }

  if (btnVerify) btnVerify.disabled = true;
  if (hint) hint.textContent = '同步手帐…';

  try {
    const verified = await apiJson('/auth/email/otp:verify', {
      method: 'POST',
      body: {
        challengeId: state.otpChallenge.challengeId,
        email,
        code,
      },
    });
    saveSession({
      userId: verified.userId,
      email,
      accessToken: verified.accessToken,
      refreshToken: verified.refreshToken,
    });
    toggleSplashEmailForm(false);
    await refreshBootstrap({ allowSeed: true });
    playSplashToCapsuleMorph();
  } catch (_) {
    if (hint) hint.textContent = '验证码有误或已过期';
  } finally {
    if (btnVerify) btnVerify.disabled = false;
  }
}

/* ==========================================================================
   5. Person Creation, Note Editing, Deletion & Real Photo Upload
   ========================================================================== */
function openNewPersonModal(defaultTier = 'crystal') {
  state.newPersonTier = VALID_TIERS.includes(defaultTier) ? defaultTier : 'crystal';
  document.querySelectorAll('[data-new-tier]').forEach((btn) => {
    btn.classList.toggle('is-active', btn.dataset.newTier === state.newPersonTier);
  });
  const nameIn = document.getElementById('newPersonNameInput');
  const roleIn = document.getElementById('newPersonRoleInput');
  if (nameIn) nameIn.value = '';
  if (roleIn) roleIn.value = '';
  personCreateModal.classList.add('is-open');
  personCreateModal.setAttribute('aria-hidden', 'false');
  setTimeout(() => nameIn && nameIn.focus(), 60);
}

function closeNewPersonModal() {
  personCreateModal.classList.remove('is-open');
  personCreateModal.setAttribute('aria-hidden', 'true');
}

async function submitNewPersonForm(e) {
  e.preventDefault();
  const nameIn = document.getElementById('newPersonNameInput');
  const roleIn = document.getElementById('newPersonRoleInput');
  const submitBtn = document.getElementById('btnSubmitNewPerson');
  const nameCn = (nameIn ? nameIn.value : '').trim();
  const roleTag = (roleIn ? roleIn.value : '').trim();
  if (!nameCn) return;

  if (submitBtn) submitBtn.disabled = true;
  try {
    const today = formatTodayTokens();
    const created = await apiJson('/persons', {
      method: 'POST',
      body: {
        nameCn,
        roleTag: roleTag || '日常往来',
        contextSub: roleTag ? `${roleTag} · 手帐一页` : '心络手帐',
        shortMemo: `${nameCn}的专属手帐页`,
        tier: state.newPersonTier,
        visualTokens: {
          folioDate: today.folioDate,
          hoboDay: today.hoboDay,
          hoboMonthWk: today.hoboMonthWk,
          ticketNo: today.ticketNo,
          stampCode: today.stampCode,
          noteSign: `— ${nameCn} · 手札`,
        },
      },
    });
    if (created && created.id) {
      await apiJson(`/persons/${created.id}/materials`, {
        method: 'POST',
        body: {
          kind: 'note',
          title: `${nameCn} · 初记`,
          body: `为${nameCn}留出的一页空白，随时记下相处里的偏好与约定。`,
          snippet: `${nameCn}的专属手帐页`,
          stampCode: today.stampCode,
          pinned: true,
        },
      });
      closeNewPersonModal();
      state.activePersonId = String(created.id);
      state.viewMode = 'person-journal';
      await refreshBootstrap();
      openPersonJournal(String(created.id));
    }
  } catch (_) {
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

function openNoteEditModal() {
  const p = PEOPLE_DB[state.activePersonId];
  if (!p) return;
  state.deleteConfirmArmed = false;
  const delBtn = document.getElementById('btnDeletePerson');
  if (delBtn) delBtn.textContent = '移除';
  document.getElementById('noteEditTitle').textContent = p.nameCn;
  document.getElementById('editShortMemoInput').value = p.shortMemo || '';
  document.getElementById('editNoteBodyInput').value = p.noteBody || '';
  noteEditModal.classList.add('is-open');
  noteEditModal.setAttribute('aria-hidden', 'false');
}

function closeNoteEditModal() {
  noteEditModal.classList.remove('is-open');
  noteEditModal.setAttribute('aria-hidden', 'true');
  state.deleteConfirmArmed = false;
}

async function submitNoteEditForm(e) {
  e.preventDefault();
  const p = PEOPLE_DB[state.activePersonId];
  if (!p) return;
  const memoVal = document.getElementById('editShortMemoInput').value.trim();
  const bodyVal = document.getElementById('editNoteBodyInput').value.trim();
  const saveBtn = document.getElementById('btnSaveNoteEdit');
  if (saveBtn) saveBtn.disabled = true;

  try {
    const nextTraits = memoVal ? [memoVal, ...(p.traits || []).filter((t) => t !== memoVal).slice(0, 2)] : p.traits;
    await apiJson(`/persons/${p.id}`, {
      method: 'PATCH',
      body: {
        shortMemo: memoVal || p.shortMemo,
        traits: nextTraits,
      },
    });
    if (bodyVal) {
      if (p.primaryNoteId && UUID_RE.test(p.primaryNoteId)) {
        await apiJson(`/materials/${p.primaryNoteId}`, {
          method: 'PATCH',
          body: {
            body: bodyVal,
            snippet: (memoVal || bodyVal).slice(0, 120),
          },
        });
      } else {
        await apiJson(`/persons/${p.id}/materials`, {
          method: 'POST',
          body: {
            kind: 'note',
            title: `${p.nameCn} · 手札`,
            body: bodyVal,
            snippet: (memoVal || bodyVal).slice(0, 120),
            pinned: true,
          },
        });
      }
    }
    closeNoteEditModal();
    await refreshBootstrap();
    openPersonJournal(p.id);
  } catch (_) {
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function handleDeleteActivePerson() {
  const p = PEOPLE_DB[state.activePersonId];
  if (!p) return;
  const delBtn = document.getElementById('btnDeletePerson');
  if (!state.deleteConfirmArmed) {
    state.deleteConfirmArmed = true;
    if (delBtn) delBtn.textContent = '确认移除';
    return;
  }
  if (delBtn) delBtn.disabled = true;
  try {
    await apiJson(`/persons/${p.id}`, { method: 'DELETE' });
    closeNoteEditModal();
    state.activePersonId = null;
    await refreshBootstrap();
    if (state.coreIds.length === 0 && state.casualIds.length === 0) {
      openNewPersonModal('gold');
    }
  } catch (_) {
  } finally {
    if (delBtn) delBtn.disabled = false;
  }
}

function compressImageFileToBlob(file, maxEdge = 1280, squareCrop = false) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d');
      if (squareCrop) {
        const side = Math.min(img.width, img.height);
        const sx = (img.width - side) * 0.5;
        const sy = (img.height - side) * 0.5;
        canvas.width = maxEdge;
        canvas.height = maxEdge;
        ctx.drawImage(img, sx, sy, side, side, 0, 0, maxEdge, maxEdge);
      } else {
        const scale = Math.min(1, maxEdge / Math.max(img.width, img.height));
        canvas.width = Math.max(1, Math.round(img.width * scale));
        canvas.height = Math.max(1, Math.round(img.height * scale));
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      }
      canvas.toBlob(
        (blob) => (blob ? resolve(blob) : reject(new Error('Image encode failed'))),
        'image/jpeg',
        0.88
      );
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('Image load failed'));
    };
    img.src = url;
  });
}

async function handlePersonPhotoSelected(file) {
  const p = PEOPLE_DB[state.activePersonId];
  if (!file || !p) return;
  try {
    const [mainBlob, avatarBlob] = await Promise.all([
      compressImageFileToBlob(file, 1360, false),
      compressImageFileToBlob(file, 256, true),
    ]);

    const mainForm = new FormData();
    mainForm.append('file', mainBlob, 'polaroid.jpg');
    await apiJson(`/persons/${p.id}/photos/main`, {
      method: 'POST',
      body: mainForm,
    });

    const avForm = new FormData();
    avForm.append('file', avatarBlob, 'avatar.jpg');
    await apiJson(`/persons/${p.id}/photos/avatar`, {
      method: 'POST',
      body: avForm,
    });

    await refreshBootstrap();
    openPersonJournal(p.id);
  } catch (_) {}
}

/* ==========================================================================
   6. Center Capsule Interactions (Tap & Horizontal Scrub)
   ========================================================================== */
function getCapsuleButtons() {
  return Array.from(capsuleExpandedItems.querySelectorAll('.capsule-obj-avatar'));
}

export function setCapsuleExpanded(expanded) {
  state.capsuleExpanded = Boolean(expanded);
  if (state.capsuleExpanded) {
    dockCenterCapsule.classList.remove('is-pure-landscape');
    setArrowPopoverOpen(false);
  }
  dockCenterCapsule.classList.toggle('is-expanded', state.capsuleExpanded);
  capsuleExpandedItems.setAttribute('aria-hidden', String(!state.capsuleExpanded));

  if (!state.capsuleExpanded) {
    clearCapsuleScrubMagnification();
  }
}

function clearCapsuleScrubMagnification() {
  getCapsuleButtons().forEach((btn) => {
    btn.style.removeProperty('--scrub-scale');
    btn.style.removeProperty('--scrub-lift');
    btn.classList.remove('is-scrub-focus');
  });
}

function updateCapsuleScrubAtX(clientX) {
  const btns = getCapsuleButtons();
  let closestBtn = null;
  let minDist = Infinity;

  btns.forEach((btn) => {
    const r = btn.getBoundingClientRect();
    const cx = r.left + r.width * 0.5;
    const dist = Math.abs(clientX - cx);
    if (dist < minDist) {
      minDist = dist;
      closestBtn = btn;
    }
    const proximity = Math.max(0, 1 - dist / 60);
    btn.style.setProperty('--scrub-scale', (1 + proximity * 0.24).toFixed(3));
    btn.style.setProperty('--scrub-lift', `${(-proximity * 7).toFixed(2)}px`);
  });

  btns.forEach((btn) => {
    btn.classList.toggle('is-scrub-focus', btn === closestBtn && minDist < 48);
  });

  return minDist < 54 ? closestBtn : null;
}

function selectCapsuleTarget(targetKey) {
  if (!targetKey) return;
  closeDispatchModal();
  if (targetKey === '__add_person__') {
    openNewPersonModal('gold');
    return;
  }
  if (targetKey === '__casual_dir__') {
    openCasualDirectory();
  } else if (PEOPLE_DB[targetKey]) {
    openPersonJournal(targetKey);
  }
  if (state.activeScene !== 'journal') {
    switchScene('journal');
  }
}

function initCenterCapsuleInteractions() {
  let pointerDown = false;
  let activePointerId = null;
  let scrubbing = false;
  let startX = 0;
  let scrubbedBtn = null;

  dockCenterCapsule.addEventListener('dragstart', (e) => e.preventDefault());

  dockCenterCapsule.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    pointerDown = true;
    activePointerId = e.pointerId;
    scrubbing = false;
    scrubbedBtn = null;
    startX = e.clientX;
  });

  window.addEventListener('pointermove', (e) => {
    if (!pointerDown) return;
    const dx = Math.abs(e.clientX - startX);

    if (!scrubbing && dx >= 8) {
      scrubbing = true;
      try {
        if (activePointerId !== null) dockCenterCapsule.setPointerCapture(activePointerId);
      } catch (_) {}
      if (!state.capsuleExpanded) {
        setCapsuleExpanded(true);
      }
    }

    if (scrubbing && state.capsuleExpanded) {
      scrubbedBtn = updateCapsuleScrubAtX(e.clientX);
    }
  });

  const finishCapsulePointer = (e, cancelled = false) => {
    if (!pointerDown) return;
    pointerDown = false;
    try {
      if (activePointerId !== null && dockCenterCapsule.hasPointerCapture(activePointerId)) {
        dockCenterCapsule.releasePointerCapture(activePointerId);
      }
    } catch (_) {}
    activePointerId = null;

    if (cancelled) {
      scrubbing = false;
      clearCapsuleScrubMagnification();
      return;
    }

    if (scrubbing) {
      const finalBtn = updateCapsuleScrubAtX(e.clientX) || scrubbedBtn;
      if (finalBtn && finalBtn.dataset.capsuleTarget) {
        selectCapsuleTarget(finalBtn.dataset.capsuleTarget);
      }
      setTimeout(() => setCapsuleExpanded(false), 75);
      scrubbing = false;
      return;
    }

    if (!dockCenterCapsule.contains(e.target)) return;

    const tappedAvatar = e.target.closest('.capsule-obj-avatar');
    if (state.capsuleExpanded && tappedAvatar && tappedAvatar.dataset.capsuleTarget) {
      selectCapsuleTarget(tappedAvatar.dataset.capsuleTarget);
      setCapsuleExpanded(false);
    } else {
      setCapsuleExpanded(!state.capsuleExpanded);
    }
  };

  window.addEventListener('pointerup', (e) => finishCapsulePointer(e, false));
  window.addEventListener('pointercancel', (e) => finishCapsulePointer(e, true));

  document.addEventListener('pointerdown', (e) => {
    if (state.capsuleExpanded && !dockCenterCapsule.contains(e.target)) {
      setCapsuleExpanded(false);
    }
  });
}

/* ==========================================================================
   7. Split-Arc Real Voice Recording + Quick Text Capture + 5s Undo
   ========================================================================== */
let voiceWaveRaf = null;
let voiceTimerInterval = null;
let voiceStartTs = 0;
let mediaRecorder = null;
let mediaStream = null;
let audioChunks = [];
let audioMimeType = 'audio/webm';
let audioCtx = null;
let audioAnalyser = null;
let audioFreqBuf = null;
let liveMicBoost = 1;
let undoTimerId = null;

const CANCEL_RADIUS_LOCAL = 188;

function pickSupportedAudioMime() {
  if (typeof MediaRecorder === 'undefined') return '';
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
    'audio/ogg',
  ];
  for (const mime of candidates) {
    if (typeof MediaRecorder.isTypeSupported === 'function' && MediaRecorder.isTypeSupported(mime)) {
      return mime;
    }
  }
  return '';
}

function extForMime(mime) {
  const m = (mime || '').toLowerCase();
  if (m.includes('mp4') || m.includes('m4a')) return 'm4a';
  if (m.includes('ogg')) return 'ogg';
  if (m.includes('wav')) return 'wav';
  return 'webm';
}

function renderSilkWaveform(now) {
  if (!voiceSilkCtx || !voiceSilkCanvas) return;
  const w = voiceSilkCanvas.width;
  const h = voiceSilkCanvas.height;
  const midY = h * 0.5;
  const t = now * 0.0046;

  if (audioAnalyser && audioFreqBuf) {
    audioAnalyser.getByteFrequencyData(audioFreqBuf);
    let sum = 0;
    for (let i = 0; i < audioFreqBuf.length; i += 1) sum += audioFreqBuf[i];
    const avg = sum / (audioFreqBuf.length * 255);
    liveMicBoost = 0.55 + Math.min(1.35, avg * 3.2);
  } else {
    liveMicBoost = 1;
  }

  voiceSilkCtx.clearRect(0, 0, w, h);

  const isCancel = state.voiceDragZone === 'cancel';
  const isGlobal = state.voiceDragZone === 'global';
  const ribbons = isCancel
    ? [
        { color: 'rgba(255, 245, 244, 0.92)', amp: 9, freq: 0.026, speed: 0.95, lw: 2.0 },
        { color: 'rgba(255, 198, 192, 0.58)', amp: 6, freq: 0.04, speed: -0.75, lw: 1.5 },
      ]
    : isGlobal
    ? [
        { color: 'rgba(218, 150, 56, 0.88)', amp: 12, freq: 0.03, speed: 1.35, lw: 2.1 },
        { color: 'rgba(92, 168, 236, 0.65)', amp: 9, freq: 0.042, speed: -1.05, lw: 1.7 },
        { color: 'rgba(176, 128, 232, 0.45)', amp: 6, freq: 0.056, speed: 1.65, lw: 1.3 },
      ]
    : [
        { color: 'rgba(218, 142, 58, 0.86)', amp: 12, freq: 0.03, speed: 1.25, lw: 2.1 },
        { color: 'rgba(238, 116, 56, 0.48)', amp: 8.5, freq: 0.042, speed: -0.95, lw: 1.6 },
        { color: 'rgba(182, 128, 92, 0.34)', amp: 5.5, freq: 0.055, speed: 1.6, lw: 1.3 },
      ];

  ribbons.forEach((rb, rIdx) => {
    voiceSilkCtx.beginPath();
    for (let x = 0; x <= w; x += 3) {
      const envelope = Math.sin((x / w) * Math.PI);
      const y =
        midY +
        Math.sin(x * rb.freq + t * rb.speed + rIdx) *
          Math.cos(x * 0.014 - t * 0.6) *
          rb.amp *
          liveMicBoost *
          envelope;
      if (x === 0) voiceSilkCtx.moveTo(x, y);
      else voiceSilkCtx.lineTo(x, y);
    }
    voiceSilkCtx.strokeStyle = rb.color;
    voiceSilkCtx.lineWidth = rb.lw;
    voiceSilkCtx.lineCap = 'round';
    voiceSilkCtx.stroke();
  });

  if (state.voiceHolding) {
    voiceWaveRaf = requestAnimationFrame(renderSilkWaveform);
  }
}

function getTargetPersonForCurrentPage() {
  if (state.viewMode === 'casual-directory') {
    const firstId = state.casualIds[0] || state.coreIds[0];
    return PEOPLE_DB[firstId] || null;
  }
  return PEOPLE_DB[state.activePersonId] || PEOPLE_DB[state.coreIds[0]] || null;
}

export function setVoiceDragZone(zoneName) {
  const valid = ['current', 'global', 'cancel'].includes(zoneName) ? zoneName : 'current';
  state.voiceDragZone = valid;
  if (voiceHoldStage) voiceHoldStage.setAttribute('data-voice-zone', valid);
  if (viewport) viewport.setAttribute('data-voice-cancel', String(valid === 'cancel'));

  const upLabel = document.getElementById('voiceZoneUpGlobal');
  const rightLabel = document.getElementById('voiceZoneRightCurrent');
  if (upLabel) upLabel.classList.toggle('is-highlighted', valid === 'global');
  if (rightLabel) rightLabel.classList.toggle('is-highlighted', valid === 'current');
}

async function beginBrowserMicCapture() {
  audioChunks = [];
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (!state.voiceHolding) {
      stream.getTracks().forEach((tr) => tr.stop());
      return;
    }
    mediaStream = stream;
    const chosenMime = pickSupportedAudioMime();
    audioMimeType = chosenMime || 'audio/webm';
    mediaRecorder = chosenMime ? new MediaRecorder(stream, { mimeType: chosenMime }) : new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (ev) => {
      if (ev.data && ev.data.size > 0) audioChunks.push(ev.data);
    };
    mediaRecorder.start(120);

    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) {
      audioCtx = new Ctx();
      const src = audioCtx.createMediaStreamSource(stream);
      audioAnalyser = audioCtx.createAnalyser();
      audioAnalyser.fftSize = 64;
      audioFreqBuf = new Uint8Array(audioAnalyser.frequencyBinCount);
      src.connect(audioAnalyser);
    }
  } catch (_) {
    mediaRecorder = null;
    mediaStream = null;
  }
}

function stopBrowserMicCapture() {
  return new Promise((resolve) => {
    const cleanupTracks = () => {
      if (mediaStream) {
        mediaStream.getTracks().forEach((tr) => tr.stop());
        mediaStream = null;
      }
      if (audioCtx) {
        audioCtx.close().catch(() => {});
        audioCtx = null;
      }
      audioAnalyser = null;
      audioFreqBuf = null;
    };

    if (!mediaRecorder || mediaRecorder.state === 'inactive') {
      cleanupTracks();
      const blob = audioChunks.length > 0 ? new Blob(audioChunks, { type: audioMimeType }) : null;
      mediaRecorder = null;
      audioChunks = [];
      resolve(blob);
      return;
    }

    const rec = mediaRecorder;
    mediaRecorder = null;
    rec.onstop = () => {
      cleanupTracks();
      const blob = audioChunks.length > 0 ? new Blob(audioChunks, { type: rec.mimeType || audioMimeType }) : null;
      audioChunks = [];
      resolve(blob);
    };
    try {
      rec.stop();
    } catch (_) {
      cleanupTracks();
      resolve(null);
    }
  });
}

function armUndoButton(jobId, undoDeadlineIso) {
  if (undoTimerId) {
    clearTimeout(undoTimerId);
    undoTimerId = null;
  }
  if (!jobId || !undoDeadlineIso || !btnUndoVoiceCapture) {
    state.lastCaptureUndo = null;
    if (btnUndoVoiceCapture) btnUndoVoiceCapture.hidden = true;
    return;
  }
  const remainMs = Date.parse(undoDeadlineIso) - Date.now();
  if (!(remainMs > 400)) {
    state.lastCaptureUndo = null;
    btnUndoVoiceCapture.hidden = true;
    return;
  }
  state.lastCaptureUndo = { jobId: String(jobId) };
  btnUndoVoiceCapture.hidden = false;
  undoTimerId = setTimeout(() => {
    state.lastCaptureUndo = null;
    btnUndoVoiceCapture.hidden = true;
  }, remainMs);
}

async function handleUndoLastCapture() {
  if (!state.lastCaptureUndo || !state.lastCaptureUndo.jobId) return;
  const { jobId } = state.lastCaptureUndo;
  state.lastCaptureUndo = null;
  if (btnUndoVoiceCapture) btnUndoVoiceCapture.hidden = true;
  try {
    await apiJson(`/captures/${jobId}:undo`, { method: 'POST', body: {} });
    closeDispatchModal();
    await refreshBootstrap();
  } catch (_) {}
}

export function openDispatchModal(splits, jobId = null, undoDeadline = null) {
  if (!aiVoiceDispatchModal) return;
  state.lastDispatchSplits = Array.isArray(splits) ? splits : [];
  armUndoButton(jobId, undoDeadline);

  const listEl = document.getElementById('dispatchCardsList');
  listEl.innerHTML = state.lastDispatchSplits
    .map((item) => {
      const pid = String(item.personId);
      const p = PEOPLE_DB[pid];
      const nameCn = (p && p.nameCn) || item.personName || '新人物';
      const mono = (p && p.monogram) || nameCn.slice(0, 1);
      const sealBg = (p && p.sealBg) || 'linear-gradient(145deg, #fcf4e6 0%, #efd9b8 100%)';
      const sealColor = (p && p.sealColor) || '#694418';
      return `
        <button type="button" class="dispatch-person-card" data-jump-split-person="${escapeHtml(pid)}">
          <span class="dispatch-monogram" style="background:${escapeHtml(sealBg)}; color:${escapeHtml(sealColor)};">${escapeHtml(mono)}</span>
          <span class="dispatch-card-body">
            <span class="dispatch-card-top">
              <strong>${escapeHtml(nameCn)}</strong>
            </span>
            <small>${escapeHtml(item.body)}</small>
          </span>
          <span class="dispatch-jump-lbl" aria-hidden="true">›</span>
        </button>
      `;
    })
    .join('');

  aiVoiceDispatchModal.classList.add('is-open');
  aiVoiceDispatchModal.setAttribute('aria-hidden', 'false');
}

export function closeDispatchModal() {
  if (!aiVoiceDispatchModal) return;
  aiVoiceDispatchModal.classList.remove('is-open');
  aiVoiceDispatchModal.setAttribute('aria-hidden', 'true');
}

function openQuickCaptureSheet() {
  quickCaptureSheet.classList.add('is-open');
  quickCaptureSheet.setAttribute('aria-hidden', 'false');
  const inp = document.getElementById('quickCaptureInput');
  if (inp) {
    inp.value = '';
    setTimeout(() => inp.focus(), 50);
  }
}

function closeQuickCaptureSheet() {
  quickCaptureSheet.classList.remove('is-open');
  quickCaptureSheet.setAttribute('aria-hidden', 'true');
}

async function submitCaptureToBackend({ audioBlob = null, text = null, routeMode = 'current' }) {
  const targetP = getTargetPersonForCurrentPage();
  const activePersonId = targetP && UUID_RE.test(targetP.id) ? targetP.id : null;

  dockVoiceOrbBtn.classList.add('is-processing');
  try {
    let resp;
    if (audioBlob && audioBlob.size > 512) {
      const ext = extForMime(audioBlob.type);
      const fd = new FormData();
      fd.append('audio', audioBlob, `voice.${ext}`);
      fd.append('routeMode', routeMode);
      if (activePersonId) fd.append('activePersonId', activePersonId);
      resp = await apiJson('/voice/ingest', {
        method: 'POST',
        body: fd,
      });
    } else if (text && text.trim()) {
      resp = await apiJson('/voice/ingest', {
        method: 'POST',
        body: {
          source: 'text',
          text: text.trim(),
          routeMode,
          activePersonId,
        },
      });
    } else {
      return;
    }

    await refreshBootstrap();

    const splits = (resp && Array.isArray(resp.splits) ? resp.splits : []);
    if (routeMode === 'global' && splits.length > 0) {
      openDispatchModal(splits, resp.jobId, resp.undoDeadline);
    } else if (splits.length > 1) {
      openDispatchModal(splits, resp.jobId, resp.undoDeadline);
    } else if (splits.length === 1) {
      const targetId = String(splits[0].personId);
      if (PEOPLE_DB[targetId]) {
        openPersonJournal(targetId);
      }
      const charm = document.getElementById('artifactVoiceCharm');
      if (charm) {
        charm.classList.remove('is-freshly-pinned');
        void charm.offsetWidth;
        charm.classList.add('is-freshly-pinned');
        setTimeout(() => charm.classList.remove('is-freshly-pinned'), 1400);
      }
    }
  } catch (_) {
  } finally {
    dockVoiceOrbBtn.classList.remove('is-processing');
  }
}

export function startVoiceHold(initialZone = 'current') {
  if (state.voiceHolding) return;
  setCapsuleExpanded(false);
  setArrowPopoverOpen(false);
  closeDispatchModal();
  closeQuickCaptureSheet();

  state.voiceHolding = true;
  voiceStartTs = performance.now();
  if (viewport) viewport.setAttribute('data-voice-holding', 'true');

  setVoiceDragZone(initialZone);

  dockVoiceOrbBtn.classList.add('is-holding');
  voiceHoldStage.classList.add('is-active');
  voiceHoldStage.setAttribute('aria-hidden', 'false');

  if (voiceHoldTimerEl) voiceHoldTimerEl.textContent = '0:01';
  if (voiceTimerInterval) clearInterval(voiceTimerInterval);
  voiceTimerInterval = setInterval(() => {
    const secs = Math.max(1, Math.min(59, Math.floor((performance.now() - voiceStartTs) / 1000) + 1));
    if (voiceHoldTimerEl) voiceHoldTimerEl.textContent = `0:${String(secs).padStart(2, '0')}`;
  }, 300);

  if (voiceWaveRaf) cancelAnimationFrame(voiceWaveRaf);
  voiceWaveRaf = requestAnimationFrame(renderSilkWaveform);

  beginBrowserMicCapture();
}

export function cancelVoiceHoldSession() {
  if (!state.voiceHolding) return;
  state.voiceHolding = false;

  dockVoiceOrbBtn.classList.remove('is-holding');
  voiceHoldStage.classList.remove('is-active');
  voiceHoldStage.setAttribute('aria-hidden', 'true');
  if (viewport) {
    viewport.removeAttribute('data-voice-holding');
    viewport.removeAttribute('data-voice-cancel');
  }
  if (voiceDragPuck) voiceDragPuck.classList.remove('is-dragging');

  if (voiceTimerInterval) {
    clearInterval(voiceTimerInterval);
    voiceTimerInterval = null;
  }
  if (voiceWaveRaf) {
    cancelAnimationFrame(voiceWaveRaf);
    voiceWaveRaf = null;
  }
}

async function finishVoiceHoldAndRoute() {
  if (!state.voiceHolding) return;
  const heldMs = performance.now() - voiceStartTs;
  const chosenZone = state.voiceDragZone;
  cancelVoiceHoldSession();
  const audioBlob = await stopBrowserMicCapture();

  if (chosenZone === 'cancel') {
    return;
  }

  if (heldMs < 400 || !audioBlob || audioBlob.size <= 512) {
    openQuickCaptureSheet();
    return;
  }

  await submitCaptureToBackend({
    audioBlob,
    routeMode: chosenZone === 'global' ? 'global' : 'current',
  });
}

function initDirectionalVoiceInteractions() {
  let draggingFromOrb = false;

  const updatePointerDrag = (clientX, clientY) => {
    const vpRect = viewport.getBoundingClientRect();
    const orbRect = dockVoiceOrbBtn.getBoundingClientRect();
    const invScale = vpRect.width > 0 ? viewport.offsetWidth / vpRect.width : 1;

    const originX = (orbRect.left + orbRect.width * 0.5 - vpRect.left) * invScale;
    const originY = (orbRect.top + orbRect.height * 0.5 - vpRect.top) * invScale;
    const curX = (clientX - vpRect.left) * invScale;
    const curY = (clientY - vpRect.top) * invScale;

    const dx = curX - originX;
    const upY = originY - curY;
    const dist = Math.hypot(dx, upY);

    if (dist > 8 && voiceDragPuck) {
      voiceDragPuck.classList.add('is-dragging');
      voiceDragPuck.style.left = `${curX.toFixed(1)}px`;
      voiceDragPuck.style.top = `${curY.toFixed(1)}px`;
    }

    if (dist > CANCEL_RADIUS_LOCAL) {
      setVoiceDragZone('cancel');
      return;
    }

    if (dist > 26) {
      const angleDeg = (Math.atan2(upY, dx) * 180) / Math.PI;
      if (angleDeg > 46 && angleDeg <= 145) {
        setVoiceDragZone('global');
        return;
      }
    }

    setVoiceDragZone('current');
  };

  dockVoiceOrbBtn.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    draggingFromOrb = true;
    try {
      dockVoiceOrbBtn.setPointerCapture(e.pointerId);
    } catch (_) {}
    startVoiceHold('current');
    updatePointerDrag(e.clientX, e.clientY);
  });

  window.addEventListener('pointermove', (e) => {
    if (!draggingFromOrb || !state.voiceHolding) return;
    updatePointerDrag(e.clientX, e.clientY);
  });

  window.addEventListener('pointerup', () => {
    if (!draggingFromOrb) return;
    draggingFromOrb = false;
    finishVoiceHoldAndRoute();
  });

  window.addEventListener('pointercancel', () => {
    if (!draggingFromOrb) return;
    draggingFromOrb = false;
    cancelVoiceHoldSession();
    stopBrowserMicCapture();
  });

  document.querySelectorAll('[data-trigger-voice-route]').forEach((labelBtn) => {
    labelBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      setVoiceDragZone(labelBtn.dataset.triggerVoiceRoute);
      finishVoiceHoldAndRoute();
    });
  });

  document.getElementById('voiceHoldScrim').addEventListener('click', () => {
    cancelVoiceHoldSession();
    stopBrowserMicCapture();
  });
}

/* ==========================================================================
   8. Scene 6: Real Muse Cupid Companion Chat (History + Gemini 2.5 Flash SSE)
   ========================================================================== */
let currentDrawerMaterials = [];

function resolveChatTargetPerson() {
  if (state.viewMode === 'person-journal' && PEOPLE_DB[state.activePersonId]) {
    return PEOPLE_DB[state.activePersonId];
  }
  const fallbackId = state.casualIds[0] || state.coreIds[0] || state.activePersonId;
  return fallbackId ? PEOPLE_DB[fallbackId] : null;
}

function renderDrawerMaterials(materialsArray) {
  currentDrawerMaterials = Array.isArray(materialsArray) ? materialsArray : [];
  state.selectedMaterialIds = [];
  syncMultimodalComposerUI();

  if (!drawerMaterialsList) return;
  drawerMaterialsList.innerHTML = currentDrawerMaterials
    .map((mat) => {
      const visualHtml =
        mat.type === 'photo'
          ? `<img src="${escapeHtml(mat.thumb)}" alt="${escapeHtml(mat.title)}" class="drawer-mat-thumb" />`
          : `<span class="drawer-mat-icon-box" aria-hidden="true">${escapeHtml(mat.icon || '✎')}</span>`;
      return `
        <button type="button" class="drawer-mat-card" data-mat-id="${escapeHtml(mat.id)}">
          ${visualHtml}
          <span class="drawer-mat-meta">
            <strong>${escapeHtml(mat.title)}</strong>
            <small>${escapeHtml(mat.snippet)}</small>
          </span>
          <span class="drawer-mat-check" aria-hidden="true">✓</span>
        </button>
      `;
    })
    .join('');
}

export function setMaterialDrawerOpen(open) {
  state.drawerOpen = Boolean(open);
  if (!chatMaterialDrawer || !chatMaterialScrim) return;
  chatMaterialDrawer.style.removeProperty('transform');
  chatMaterialDrawer.style.removeProperty('transition');
  chatMaterialDrawer.classList.toggle('is-open', state.drawerOpen);
  chatMaterialDrawer.setAttribute('aria-hidden', String(!state.drawerOpen));
  chatMaterialScrim.classList.toggle('is-open', state.drawerOpen);
}

export function toggleMaterialSelection(matId) {
  const exists = state.selectedMaterialIds.includes(matId);
  state.selectedMaterialIds = exists
    ? state.selectedMaterialIds.filter((id) => id !== matId)
    : [...state.selectedMaterialIds, matId];
  syncMultimodalComposerUI();
}

function syncMultimodalComposerUI() {
  document.querySelectorAll('.drawer-mat-card').forEach((card) => {
    card.classList.toggle('is-selected', state.selectedMaterialIds.includes(card.dataset.matId));
  });
  const count = state.selectedMaterialIds.length;
  if (drawerSelectedCount) {
    drawerSelectedCount.hidden = count === 0;
    drawerSelectedCount.textContent = `${count}`;
  }
  if (!chatAttachedTray) return;
  if (count === 0) {
    chatAttachedTray.hidden = true;
    chatAttachedTray.innerHTML = '';
    return;
  }
  const selectedMats = state.selectedMaterialIds
    .map((id) => currentDrawerMaterials.find((m) => m.id === id))
    .filter(Boolean);
  chatAttachedTray.hidden = false;
  chatAttachedTray.innerHTML = selectedMats
    .map((mat) => {
      const miniThumb =
        mat.type === 'photo'
          ? `<img src="${escapeHtml(mat.thumb)}" alt="" />`
          : `<span class="attached-chip-badge">${escapeHtml(mat.icon || '✎')}</span>`;
      return `
        <span class="attached-chip" data-chip-id="${escapeHtml(mat.id)}">
          ${miniThumb}
          <span class="attached-chip-title">${escapeHtml(mat.title)}</span>
          <button type="button" class="attached-chip-remove" data-remove-mat="${escapeHtml(mat.id)}" aria-label="移除">✕</button>
        </span>
      `;
    })
    .join('');
}

function buildWelcomeGreetingForPerson(p) {
  if (p.tier === 'silver') {
    return {
      main: `关于「${p.nameCn}」的边界留档我都替你收稳啦。遇到拿不准怎么回绝或留痕的时刻，随时丢给我。`,
      whisper: '温和而笃定地守住分寸，不必替别人的越界内耗。',
    };
  }
  if (p.tier === 'crystal') {
    return {
      main: `「${p.nameCn}」的往来偏好都在手帐里记好啦。要发邮件、约时间或是挑伴手礼，随时问我一声。`,
      whisper: '日常相处点到即止、礼数周全就最省心啦。',
    };
  }
  return {
    main: `「${p.nameCn}」的小喜好和约定我都替你记在心上啦。今天想聊聊见面的小心思，还是顺手翻翻手帐素材？`,
    whisper: '点击左下角 ＋ 或从右侧左滑，可以随时调取 TA 的手帐便签。',
  };
}

function renderSingleChatMessageNode(msg, personMaterials) {
  if (msg.role === 'user') {
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble user';
    const refIds = Array.isArray(msg.referencedMaterialIds) ? msg.referencedMaterialIds.map(String) : [];
    const attached = refIds
      .map((id) => personMaterials.find((m) => m.id === id))
      .filter(Boolean);
    if (attached.length > 0) {
      const attWrap = document.createElement('div');
      attWrap.className = 'bubble-multimodal-attachments';
      attWrap.innerHTML = attached
        .map((mat) => {
          const media =
            mat.type === 'photo'
              ? `<img src="${escapeHtml(mat.thumb)}" alt="" />`
              : `<span class="bubble-att-badge-icon">${escapeHtml(mat.icon || '✎')}</span>`;
          return `<span class="bubble-att-card">${media}<span class="bubble-att-label">${escapeHtml(mat.title)}</span></span>`;
        })
        .join('');
      bubble.appendChild(attWrap);
    }
    const textDiv = document.createElement('div');
    textDiv.textContent = msg.content || '';
    bubble.appendChild(textDiv);
    return bubble;
  }

  const aiBubble = document.createElement('div');
  aiBubble.className = 'chat-bubble ai';
  const mainDiv = document.createElement('div');
  mainDiv.textContent = msg.content || '';
  aiBubble.appendChild(mainDiv);
  if (msg.whisperNote) {
    const wDiv = document.createElement('div');
    wDiv.className = 'chat-whisper-note';
    wDiv.textContent = msg.whisperNote;
    aiBubble.appendChild(wDiv);
  }
  return aiBubble;
}

async function loadChatSceneForActiveContext() {
  const p = resolveChatTargetPerson();
  if (!p) return;
  state.chatPersonId = p.id;
  state.clearChatArmed = false;
  const clearBtn = document.getElementById('btnClearChatHistory');
  if (clearBtn) clearBtn.classList.remove('is-confirming');

  const now = new Date();
  const timeEl = document.getElementById('chatTimeDivider');
  if (timeEl) {
    timeEl.textContent = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  }

  document.getElementById('aiChatHeaderName').textContent = '丘比特';
  chatTextInput.placeholder = '嘿，丘比特…';
  document.getElementById('drawerOwnerTitle').textContent = p.nameCn;
  renderDrawerMaterials(p.materials || []);

  const msgContainer = document.getElementById('aiChatMessages');
  msgContainer.innerHTML = '';

  try {
    const page = await apiJson(`/persons/${p.id}/chat/messages?limit=50&order=asc`);
    if (state.chatPersonId !== p.id) return;
    const items = (page && Array.isArray(page.items)) ? page.items : [];
    if (items.length === 0) {
      const welcome = buildWelcomeGreetingForPerson(p);
      const aiBubble = document.createElement('div');
      aiBubble.className = 'chat-bubble ai';
      aiBubble.innerHTML = `
        <div>${escapeHtml(welcome.main)}</div>
        <div class="chat-whisper-note">${escapeHtml(welcome.whisper)}</div>
      `;
      msgContainer.appendChild(aiBubble);
    } else {
      items.forEach((m) => {
        msgContainer.appendChild(renderSingleChatMessageNode(m, p.materials || []));
      });
    }
    msgContainer.scrollTop = msgContainer.scrollHeight;
  } catch (_) {}
}

function renderCrisisCardBubble(card) {
  const wrap = document.createElement('div');
  wrap.className = 'chat-crisis-card';
  const hotlinesHtml = (card.hotlines || [])
    .map((h) => {
      const name = h.name || h.label || '援助热线';
      const phone = h.phone || h.number || '';
      return `<a class="chat-crisis-hotline" href="tel:${escapeHtml(phone)}"><span>${escapeHtml(name)}</span><span>${escapeHtml(phone)}</span></a>`;
    })
    .join('');
  wrap.innerHTML = `
    <strong>${escapeHtml(card.title || '先让我陪着你')}</strong>
    <p>${escapeHtml(card.careMessage || '')}</p>
    <div class="chat-crisis-hotlines">${hotlinesHtml}</div>
  `;
  return wrap;
}

async function sendMultimodalChatMessage() {
  if (state.chatStreaming) return;
  const p = PEOPLE_DB[state.chatPersonId] || resolveChatTargetPerson();
  if (!p) return;

  const rawText = chatTextInput.value.trim();
  const attachedMats = state.selectedMaterialIds
    .map((id) => currentDrawerMaterials.find((m) => m.id === id))
    .filter(Boolean);
  if (!rawText && attachedMats.length === 0) return;

  const finalUserText =
    rawText || `结合我选中的这${attachedMats.length > 1 ? '几份' : '份'}手帐材料，帮我出出主意吧？`;
  const validMaterialUuids = attachedMats.map((m) => m.id).filter((id) => UUID_RE.test(id));

  const msgContainer = document.getElementById('aiChatMessages');
  const userBubble = document.createElement('div');
  userBubble.className = 'chat-bubble user';

  if (attachedMats.length > 0) {
    const attWrap = document.createElement('div');
    attWrap.className = 'bubble-multimodal-attachments';
    attWrap.innerHTML = attachedMats
      .map((mat) => {
        const media =
          mat.type === 'photo'
            ? `<img src="${escapeHtml(mat.thumb)}" alt="" />`
            : `<span class="bubble-att-badge-icon">${escapeHtml(mat.icon || '✎')}</span>`;
        return `<span class="bubble-att-card">${media}<span class="bubble-att-label">${escapeHtml(mat.title)}</span></span>`;
      })
      .join('');
    userBubble.appendChild(attWrap);
  }

  const textNode = document.createElement('div');
  textNode.textContent = finalUserText;
  userBubble.appendChild(textNode);
  msgContainer.appendChild(userBubble);

  chatTextInput.value = '';
  state.selectedMaterialIds = [];
  syncMultimodalComposerUI();
  setMaterialDrawerOpen(false);

  const aiBubble = document.createElement('div');
  aiBubble.className = 'chat-bubble ai';
  const bodyDiv = document.createElement('div');
  bodyDiv.innerHTML = `<span class="chat-stream-dots" aria-hidden="true"><i></i><i></i><i></i></span>`;
  aiBubble.appendChild(bodyDiv);
  msgContainer.appendChild(aiBubble);
  msgContainer.scrollTop = msgContainer.scrollHeight;

  triggerCupidCompanionGreet();
  state.chatStreaming = true;

  let streamedText = '';
  let dotsCleared = false;

  try {
    const resp = await apiFetch(`/persons/${p.id}/chat/stream`, {
      method: 'POST',
      headers: { Accept: 'text/event-stream' },
      body: {
        message: finalUserText,
        referencedMaterialIds: validMaterialUuids,
        clientMessageId: makeUuid(),
      },
    });

    if (!resp.ok || !resp.body) {
      throw new Error(`HTTP ${resp.status}`);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    const handleSseBlock = (block) => {
      const lines = block.split(/\r?\n/);
      let evName = 'message';
      const dataLines = [];
      for (const line of lines) {
        if (line.startsWith('event:')) {
          evName = line.slice(6).trim();
        } else if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).trim());
        }
      }
      if (dataLines.length === 0) return;
      let payload = null;
      try {
        payload = JSON.parse(dataLines.join('\n'));
      } catch (_) {
        return;
      }

      if (evName === 'delta' && payload && payload.text) {
        if (!dotsCleared) {
          dotsCleared = true;
          bodyDiv.textContent = '';
        }
        streamedText += payload.text;
        bodyDiv.textContent = streamedText;
        msgContainer.scrollTop = msgContainer.scrollHeight;
      } else if (evName === 'whisper' && payload && payload.text) {
        const wDiv = document.createElement('div');
        wDiv.className = 'chat-whisper-note';
        wDiv.textContent = payload.text;
        aiBubble.appendChild(wDiv);
        msgContainer.scrollTop = msgContainer.scrollHeight;
      } else if (evName === 'crisis' && payload) {
        aiBubble.replaceWith(renderCrisisCardBubble(payload));
        msgContainer.scrollTop = msgContainer.scrollHeight;
      } else if (evName === 'error') {
        if (!streamedText) {
          bodyDiv.textContent = '丘比特刚刚走神了一小会儿，再和我说一次好吗？';
        }
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sepIdx = buffer.indexOf('\n\n');
      while (sepIdx !== -1) {
        const block = buffer.slice(0, sepIdx);
        buffer = buffer.slice(sepIdx + 2);
        if (block.trim()) handleSseBlock(block);
        sepIdx = buffer.indexOf('\n\n');
      }
    }
    if (buffer.trim()) handleSseBlock(buffer);
    triggerCupidCompanionGreet();
  } catch (_) {
    if (!streamedText) {
      bodyDiv.textContent = '网络微风轻晃了一下，稍后再试一次吧。';
    }
  } finally {
    state.chatStreaming = false;
  }
}

async function handleClearChatHistory() {
  const p = PEOPLE_DB[state.chatPersonId];
  if (!p) return;
  const btn = document.getElementById('btnClearChatHistory');
  if (!state.clearChatArmed) {
    state.clearChatArmed = true;
    if (btn) btn.classList.add('is-confirming');
    setTimeout(() => {
      state.clearChatArmed = false;
      if (btn) btn.classList.remove('is-confirming');
    }, 3200);
    return;
  }
  state.clearChatArmed = false;
  if (btn) btn.classList.remove('is-confirming');
  try {
    await apiJson(`/persons/${p.id}/chat/messages`, { method: 'DELETE' });
    await loadChatSceneForActiveContext();
  } catch (_) {}
}

function initSilentRightEdgeSwipeDrawer() {
  if (!aiChatFullscreen || !chatMaterialDrawer) return;
  let tracking = false;
  let mode = null;
  let startX = 0;
  let startY = 0;
  let latestDx = 0;
  const DRAWER_WIDTH = 268;

  const beginSwipe = (e, swipeMode) => {
    tracking = true;
    mode = swipeMode;
    startX = e.clientX;
    startY = e.clientY;
    latestDx = 0;
  };

  if (chatRightEdgeZone) {
    chatRightEdgeZone.addEventListener('pointerdown', (e) => {
      if (state.drawerOpen) return;
      beginSwipe(e, 'opening');
      try {
        chatRightEdgeZone.setPointerCapture(e.pointerId);
      } catch (_) {}
    });
  }

  aiChatFullscreen.addEventListener('pointerdown', (e) => {
    if (tracking) return;
    const rect = aiChatFullscreen.getBoundingClientRect();
    const distFromRight = rect.right - e.clientX;
    if (!state.drawerOpen && distFromRight <= 52 && e.clientY > rect.top + 84 && e.clientY < rect.bottom - 76) {
      beginSwipe(e, 'opening');
    } else if (state.drawerOpen && chatMaterialDrawer.contains(e.target)) {
      beginSwipe(e, 'closing');
    }
  });

  window.addEventListener('pointermove', (e) => {
    if (!tracking) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.abs(dy) > Math.abs(dx) * 1.35 && Math.abs(dx) < 18) return;

    latestDx = dx;
    if (mode === 'opening' && dx < -6) {
      const pull = Math.min(DRAWER_WIDTH, Math.abs(dx));
      chatMaterialDrawer.style.transition = 'none';
      chatMaterialDrawer.style.transform = `translateX(${Math.max(0, DRAWER_WIDTH - pull).toFixed(1)}px)`;
      chatMaterialScrim.classList.toggle('is-open', pull > 36);
    } else if (mode === 'closing' && dx > 8) {
      chatMaterialDrawer.style.transition = 'none';
      chatMaterialDrawer.style.transform = `translateX(${Math.min(DRAWER_WIDTH, dx).toFixed(1)}px)`;
    }
  });

  const endSwipe = () => {
    if (!tracking) return;
    tracking = false;
    chatMaterialDrawer.style.removeProperty('transition');
    chatMaterialDrawer.style.removeProperty('transform');
    if (mode === 'opening') setMaterialDrawerOpen(latestDx < -34);
    else if (mode === 'closing') setMaterialDrawerOpen(!(latestDx > 40));
    mode = null;
  };

  window.addEventListener('pointerup', endSwipe);
  window.addEventListener('pointercancel', endSwipe);
  if (chatMaterialScrim) chatMaterialScrim.addEventListener('click', () => setMaterialDrawerOpen(false));
}

/* ==========================================================================
   9. Scene Navigation & Complete Event Wiring
   ========================================================================== */
export function switchScene(sceneName) {
  if (morphRafId) {
    cancelAnimationFrame(morphRafId);
    morphRafId = null;
  }
  if (emergeTimerId) {
    clearTimeout(emergeTimerId);
    emergeTimerId = null;
  }
  splashMorphActor.classList.remove('is-morphing');
  dockCenterCapsule.classList.remove('is-pure-landscape');

  state.activeScene = sceneName;

  splashScreen.classList.remove('is-active', 'is-fading-copy');
  eventsFullScreen.classList.remove('is-active');
  aiChatFullscreen.classList.remove('is-active');
  moodStonePopover.classList.remove('is-open');
  setArrowPopoverOpen(false);

  if (sceneName !== 'aichat') setMaterialDrawerOpen(false);
  if (sceneName !== 'capsule-open') setCapsuleExpanded(false);
  if (sceneName !== 'voice-hold') cancelVoiceHoldSession();

  glebTriDock.style.display = sceneName === 'splash' || sceneName === 'aichat' ? 'none' : 'flex';

  if (sceneName === 'splash') {
    splashScreen.classList.add('is-active');
  } else if (sceneName === 'capsule-open') {
    setCapsuleExpanded(true);
  } else if (sceneName === 'events') {
    eventsFullScreen.classList.add('is-active');
  } else if (sceneName === 'aichat') {
    syncArrowPinAndChatCupid(false);
    aiChatFullscreen.classList.add('is-active');
    loadChatSceneForActiveContext();
  }
}

function populateStaticArrowIcons() {
  const popGold = document.getElementById('popIconGold');
  const popSilver = document.getElementById('popIconSilver');
  const popCrystal = document.getElementById('popIconCrystal');
  if (popGold) popGold.innerHTML = ARROW_SVGS.gold;
  if (popSilver) popSilver.innerHTML = ARROW_SVGS.silver;
  if (popCrystal) popCrystal.innerHTML = ARROW_SVGS.crystal;
}

function bindEvents() {
  initCenterCapsuleInteractions();
  initDirectionalVoiceInteractions();
  initSilentRightEdgeSwipeDrawer();

  // 1. Cycle 6 Curated Paper Editions by tapping top-left folio header
  document.getElementById('journalFolioWhisper').addEventListener('click', () => {
    const nextIdx = (VALID_EDITIONS.indexOf(state.edition) + 1) % VALID_EDITIONS.length;
    setJournalEdition(VALID_EDITIONS[nextIdx], true);
  });

  // 2. Muse Top-Center Medallion cycles 4 Fair Living-Skin Cupid portraits cleanly
  const museMedallion = document.getElementById('museCupidMedallion');
  if (museMedallion) {
    museMedallion.addEventListener('click', () => {
      const nextIdx = (VALID_CUPID_LAYOUTS.indexOf(state.cupidLayout) + 1) % VALID_CUPID_LAYOUTS.length;
      setCupidLayout(VALID_CUPID_LAYOUTS[nextIdx], true);
    });
  }

  // 3. Top-Right Tactile Arrow Pushpin -> Gold / Silver / Crystal
  journalArrowPinBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    setArrowPopoverOpen(!state.arrowPopoverOpen);
  });
  document.querySelectorAll('[data-select-tier]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      changeActivePersonArrowTier(btn.dataset.selectTier);
    });
  });
  document.addEventListener('pointerdown', (e) => {
    if (state.arrowPopoverOpen && !topRightArrowAnchor.contains(e.target)) {
      setArrowPopoverOpen(false);
    }
  });

  // 4. Back to Casual Directory & Casual Row Click
  if (btnBackToCasualList) {
    btnBackToCasualList.addEventListener('click', () => openCasualDirectory());
  }
  if (casualEntriesStage) {
    casualEntriesStage.addEventListener('click', (e) => {
      const row = e.target.closest('[data-open-casual-person]');
      if (row) openPersonJournal(row.dataset.openCasualPerson);
    });
  }

  // 5. Splash & Seamless Email Sync Auth
  document.getElementById('btnSplashLetsGo').addEventListener('click', ensureAuthenticatedAndBootJournal);
  document.getElementById('btnToggleSplashEmail').addEventListener('click', () => toggleSplashEmailForm(true));
  document.getElementById('btnCancelSplashEmail').addEventListener('click', () => toggleSplashEmailForm(false));
  document.getElementById('btnSendOtp').addEventListener('click', handleSendEmailOtp);
  document.getElementById('splashAuthForm').addEventListener('submit', handleVerifyEmailOtp);

  // 6. Add Person & Edit Note Modals + Real Photo Upload
  document.getElementById('btnOpenNewPerson').addEventListener('click', () => openNewPersonModal('crystal'));
  document.getElementById('btnCloseNewPerson').addEventListener('click', closeNewPersonModal);
  personCreateModal.addEventListener('click', (e) => {
    if (e.target === personCreateModal) closeNewPersonModal();
  });
  document.querySelectorAll('[data-new-tier]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.newPersonTier = btn.dataset.newTier;
      document.querySelectorAll('[data-new-tier]').forEach((b) => {
        b.classList.toggle('is-active', b.dataset.newTier === state.newPersonTier);
      });
    });
  });
  document.getElementById('personCreateForm').addEventListener('submit', submitNewPersonForm);

  document.getElementById('artifactTextNote').addEventListener('click', openNoteEditModal);
  artifactDossierCard.addEventListener('click', (e) => {
    if (e.target.closest('#btnUploadPhotoOnDossier')) return;
    openNoteEditModal();
  });
  document.getElementById('btnCloseNoteEdit').addEventListener('click', closeNoteEditModal);
  noteEditModal.addEventListener('click', (e) => {
    if (e.target === noteEditModal) closeNoteEditModal();
  });
  document.getElementById('noteEditForm').addEventListener('submit', submitNoteEditForm);
  document.getElementById('btnDeletePerson').addEventListener('click', handleDeleteActivePerson);

  const triggerPhotoPicker = (e) => {
    e.stopPropagation();
    if (photoUploadInput) {
      photoUploadInput.value = '';
      photoUploadInput.click();
    }
  };
  document.getElementById('btnUploadPhotoOnPolaroid').addEventListener('click', triggerPhotoPicker);
  document.getElementById('btnUploadPhotoOnDossier').addEventListener('click', triggerPhotoPicker);
  photoUploadInput.addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) handlePersonPhotoSelected(file);
  });

  // 7. Quick Voice/Text Capture Sheet & AI Split Dispatch Modal
  quickCaptureSheet.addEventListener('click', (e) => {
    if (e.target === quickCaptureSheet) closeQuickCaptureSheet();
  });
  document.querySelectorAll('[data-quick-route]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const inp = document.getElementById('quickCaptureInput');
      const val = inp ? inp.value.trim() : '';
      if (!val) return;
      const routeMode = btn.dataset.quickRoute === 'global' ? 'global' : 'current';
      closeQuickCaptureSheet();
      submitCaptureToBackend({ text: val, routeMode });
    });
  });
  document.getElementById('quickCaptureInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const val = e.target.value.trim();
      if (!val) return;
      closeQuickCaptureSheet();
      submitCaptureToBackend({ text: val, routeMode: 'global' });
    }
  });

  document.getElementById('btnCloseDispatchModal').addEventListener('click', closeDispatchModal);
  if (btnUndoVoiceCapture) {
    btnUndoVoiceCapture.addEventListener('click', handleUndoLastCapture);
  }
  aiVoiceDispatchModal.addEventListener('click', (e) => {
    if (e.target === aiVoiceDispatchModal) closeDispatchModal();
    const jumpCard = e.target.closest('[data-jump-split-person]');
    if (jumpCard) {
      closeDispatchModal();
      openPersonJournal(jumpCard.dataset.jumpSplitPerson);
    }
  });

  // 8. Scrapbook Photo Mount & Voice Charm
  artifactPhotoMount.addEventListener('click', (e) => {
    if (e.target.closest('#btnUploadPhotoOnPolaroid')) return;
    switchScene('events');
  });
  const voiceCharm = document.getElementById('artifactVoiceCharm');
  voiceCharm.addEventListener('click', () => {
    const playing = voiceCharm.classList.toggle('is-playing');
    document.getElementById('voiceCharmPlayIcon').textContent = playing ? '❚❚' : '▶';
    if (playing) {
      setTimeout(() => {
        voiceCharm.classList.remove('is-playing');
        document.getElementById('voiceCharmPlayIcon').textContent = '▶';
      }, 2600);
    }
  });

  // 9. Muse Cupid Chat Controls
  document.getElementById('btnOpenAiOrb').addEventListener('click', () => {
    closeDispatchModal();
    switchScene('aichat');
  });
  document.getElementById('btnCloseAiChat').addEventListener('click', () => {
    switchScene('journal');
  });
  document.getElementById('btnClearChatHistory').addEventListener('click', handleClearChatHistory);

  document.getElementById('btnOpenMaterialDrawerPlus').addEventListener('click', () => {
    setMaterialDrawerOpen(!state.drawerOpen);
  });
  drawerMaterialsList.addEventListener('click', (e) => {
    const card = e.target.closest('[data-mat-id]');
    if (card) toggleMaterialSelection(card.dataset.matId);
  });
  chatAttachedTray.addEventListener('click', (e) => {
    const rmBtn = e.target.closest('[data-remove-mat]');
    if (rmBtn) toggleMaterialSelection(rmBtn.dataset.removeMat);
  });
  document.getElementById('btnSendChatMsg').addEventListener('click', sendMultimodalChatMessage);
  chatTextInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendMultimodalChatMessage();
  });

  // 10. Events Flip Cards & Mood Stone Popover + Account Controls
  document.getElementById('btnEventsBack').addEventListener('click', () => switchScene('journal'));
  ['cinemaCard1', 'cinemaCard2'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', () => el.classList.toggle('is-flipped'));
  });

  document.getElementById('btnToggleMiniStone').addEventListener('click', () => {
    syncAccountLabel();
    moodStonePopover.classList.add('is-open');
    drawStoneFrame(currentStoneFrame);
  });
  document.getElementById('btnCloseMiniStone').addEventListener('click', () => {
    moodStonePopover.classList.remove('is-open');
  });
  moodStonePopover.addEventListener('click', (e) => {
    if (e.target === moodStonePopover) moodStonePopover.classList.remove('is-open');
  });
  document.getElementById('btnOpenSplashCover').addEventListener('click', () => {
    moodStonePopover.classList.remove('is-open');
    switchScene('splash');
  });
  document.getElementById('btnSwitchAccount').addEventListener('click', async () => {
    moodStonePopover.classList.remove('is-open');
    if (state.session && state.session.accessToken) {
      apiJson('/auth/logout', {
        method: 'POST',
        body: { refreshToken: state.session.refreshToken },
      }).catch(() => {});
    }
    saveSession(null);
    switchScene('splash');
    toggleSplashEmailForm(true);
  });

  if (stoneSliderWrap) {
    let dragging = false;
    const updateFromClientX = (clientX) => {
      const rect = stoneSliderWrap.getBoundingClientRect();
      setStoneByPercent(((clientX - rect.left) / rect.width) * 100);
    };
    stoneSliderWrap.addEventListener('pointerdown', (e) => {
      dragging = true;
      stoneSliderWrap.setPointerCapture(e.pointerId);
      updateFromClientX(e.clientX);
    });
    stoneSliderWrap.addEventListener('pointermove', (e) => {
      if (dragging) updateFromClientX(e.clientX);
    });
    stoneSliderWrap.addEventListener('pointerup', () => {
      dragging = false;
    });
  }
}

/* ==========================================================================
   10. Boot Application
   ========================================================================== */
async function bootApp() {
  preloadAllAssets();
  populateStaticArrowIcons();
  setJournalEdition('cotton', false);
  setCupidLayout('a', false);
  syncAccountLabel();
  bindEvents();

  if (state.session && state.session.accessToken) {
    try {
      await refreshBootstrap({ allowSeed: true });
      switchScene('journal');
      return;
    } catch (_) {
      saveSession(null);
    }
  }
  switchScene('splash');
}

bootApp();
