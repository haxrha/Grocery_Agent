(() => {
  const intro = document.getElementById('opening-intro');
  const skip = document.getElementById('intro-skip');
  const replay = document.getElementById('intro-replay');
  const status = document.getElementById('opening-status');
  const saveMoney = document.getElementById('save-money');
  const saveTime = document.getElementById('save-time');
  const caption = document.getElementById('big-caption');
  const goalMeter = document.getElementById('goal-meter');
  const goalLabel = document.getElementById('goal-label');
  const goalFill = document.getElementById('goal-fill');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const appSurfaces = [
    document.querySelector('header'),
    document.querySelector('main'),
    document.querySelector('footer'),
    document.getElementById('order-overlay'),
  ].filter(Boolean);
  let timers = [];

  const clearTimers = () => {
    timers.forEach(window.clearTimeout);
    timers = [];
  };

  const at = (delay, fn) => timers.push(window.setTimeout(fn, delay));

  const setAppInert = value => {
    appSurfaces.forEach(surface => { surface.inert = value; });
  };

  const setSavings = (money, minutes) => {
    saveMoney.textContent = '$' + money.toFixed(2);
    saveTime.textContent = minutes + ' min';
    [saveMoney, saveTime].forEach(el => {
      el.classList.remove('bump');
      void el.offsetWidth;
      el.classList.add('bump');
    });
  };

  const setCaption = (html, stay) => {
    caption.innerHTML = html;
    caption.classList.remove('pop', 'stay');
    void caption.offsetWidth;
    caption.classList.add(stay ? 'stay' : 'pop');
  };

  const finish = () => {
    clearTimers();
    intro.classList.add('leaving');
    timers.push(window.setTimeout(() => {
      intro.hidden = true;
      intro.classList.remove('playing', 'leaving');
      setAppInert(false);
    }, reducedMotion.matches ? 0 : 330));
  };

  const play = () => {
    clearTimers();
    setAppInert(true);
    intro.hidden = false;
    intro.classList.remove('playing', 'leaving');
    status.textContent = '6:58 pm — the block is getting hungry…';
    saveMoney.textContent = '$0.00';
    saveTime.textContent = '0 min';
    caption.innerHTML = '';
    caption.classList.remove('pop', 'stay');
    goalMeter.classList.remove('show', 'unlocked');
    goalLabel.innerHTML = 'So close — <b>$4 to go</b> for the bulk deal!';
    goalFill.style.width = '0%';
    intro.classList.remove('final-beat');
    void intro.offsetWidth;
    intro.classList.add('playing');

    if (reducedMotion.matches) {
      setSavings(24, 45);
      timers.push(window.setTimeout(finish, 850));
      return;
    }

    // act 1 — the group chat
    at(800, () => { status.textContent = 'Everyone texts Henry their list. No app — just iMessage.'; });
    at(4300, () => { status.textContent = 'Henry: say less. One car for everybody.'; });

    // act 2 — the drive
    at(5500, () => { status.textContent = 'One route past every store — nobody leaves their couch.'; });
    at(5450, () => setCaption('One car. The whole block.'));
    at(6600, () => { setCaption('Bulk prices: <b>−$6</b>'); setSavings(6, 15); });
    at(7950, () => { setCaption('1 delivery fee ÷ 6 neighbors: <b>−$6</b>'); setSavings(12, 30); });

    // act 3 — the deal goal
    at(8550, () => {
      status.textContent = 'The group cart is $4 from the bulk discount…';
      goalMeter.classList.add('show');
      goalFill.style.width = '90%';
    });
    at(9350, () => { goalFill.style.width = '94%'; });
    at(9950, () => {
      status.textContent = 'Leo pulled through. Deal unlocked for everyone!';
      goalMeter.classList.add('unlocked');
      goalLabel.innerHTML = 'DEAL UNLOCKED — <b>−$12</b> for the block!';
      goalFill.style.width = '100%';
      setSavings(24, 45);
    });
    at(11600, () => goalMeter.classList.remove('show'));

    // act 4 — the drop-off
    at(12800, () => { status.textContent = 'Drop-off at the house. Everyone grabs their bag.'; });
    at(12750, () => intro.classList.add('final-beat'));
    at(12900, () => setCaption('<b>$24</b> + <b>45 min</b> saved. Every single run.', true));

    at(15000, finish);
  };

  skip.addEventListener('click', finish);
  replay.addEventListener('click', play);
  play();
})();
