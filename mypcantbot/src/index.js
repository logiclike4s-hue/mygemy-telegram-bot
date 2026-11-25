import 'dotenv/config';
import path from 'node:path';
import os from 'node:os';
import { unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { Telegraf, Markup } from 'telegraf';
import {
  getStatus,
  lockComputer,
  openApp,
  powerAction,
  runAllowedCommand,
  sendMediaKey,
  sendVolumeKey,
  takeScreenshot
} from './windows.js';

const token = process.env.TELEGRAM_BOT_TOKEN;
const allowedUserId = process.env.TELEGRAM_ALLOWED_USER_ID;

if (!token) throw new Error('TELEGRAM_BOT_TOKEN is required');
if (!allowedUserId) throw new Error('TELEGRAM_ALLOWED_USER_ID is required');

const bot = new Telegraf(token);
const pollingAbortController = new AbortController();

bot.use(async (ctx, next) => {
  const userId = ctx.from?.id?.toString();
  if (userId !== allowedUserId) {
    await ctx.reply('Access denied.');
    return;
  }
  await next();
});

function mainMenu() {
  return Markup.inlineKeyboard([
    [Markup.button.callback('Status', 'status'), Markup.button.callback('Screenshot', 'screenshot')],
    [Markup.button.callback('Volume -', 'volume_down'), Markup.button.callback('Mute', 'mute'), Markup.button.callback('Volume +', 'volume_up')],
    [Markup.button.callback('Prev', 'media_prev'), Markup.button.callback('Play/Pause', 'media_play_pause'), Markup.button.callback('Next', 'media_next')],
    [Markup.button.callback('Lock', 'lock'), Markup.button.callback('Sleep', 'confirm_sleep')],
    [Markup.button.callback('Restart', 'confirm_restart'), Markup.button.callback('Shutdown', 'confirm_shutdown')],
    [Markup.button.callback('Open app', 'open_menu'), Markup.button.callback('Run', 'run_menu')]
  ]);
}

bot.start(async (ctx) => ctx.reply('PC Control', mainMenu()));
bot.command('menu', async (ctx) => ctx.reply('PC Control', mainMenu()));

bot.action('status', async (ctx) => {
  await ctx.answerCbQuery();
  await ctx.reply(await getStatus());
});

bot.action('screenshot', async (ctx) => {
  await ctx.answerCbQuery('Capturing...');
  const screenshotPath = path.join(os.tmpdir(), `mypc-${randomUUID()}.png`);
  try {
    await takeScreenshot(screenshotPath);
    await ctx.replyWithPhoto({ source: screenshotPath });
  } catch (error) {
    await ctx.reply(`Screenshot failed: ${error.message}`);
  } finally {
    await unlink(screenshotPath).catch(() => {});
  }
});

for (const action of ['mute', 'volume_down', 'volume_up']) {
  bot.action(action, async (ctx) => {
    await ctx.answerCbQuery();
    await sendVolumeKey(action);
  });
}

for (const [callback, mediaAction] of [
  ['media_prev', 'prev'],
  ['media_play_pause', 'play_pause'],
  ['media_next', 'next']
]) {
  bot.action(callback, async (ctx) => {
    await ctx.answerCbQuery();
    await sendMediaKey(mediaAction);
  });
}

bot.action('lock', async (ctx) => {
  await ctx.answerCbQuery();
  await lockComputer();
  await ctx.reply('Computer locked.');
});

bot.action('open_menu', async (ctx) => {
  await ctx.answerCbQuery();
  await ctx.reply('Open application:', Markup.inlineKeyboard([
    [Markup.button.callback('Calculator', 'open_calculator'), Markup.button.callback('Notepad', 'open_notepad')],
    [Markup.button.callback('Explorer', 'open_explorer'), Markup.button.callback('Task Manager', 'open_task_manager')],
    [Markup.button.callback('Control Panel', 'open_control_panel'), Markup.button.callback('Browser', 'open_browser')],
    [Markup.button.callback('Back', 'menu')]
  ]));
});

for (const name of ['calculator', 'notepad', 'explorer', 'task_manager', 'control_panel', 'browser']) {
  bot.action(`open_${name}`, async (ctx) => {
    await ctx.answerCbQuery();
    await openApp(name);
  });
}

bot.action('run_menu', async (ctx) => {
  await ctx.answerCbQuery();
  await ctx.reply('Allowed read-only command:', Markup.inlineKeyboard([
    [Markup.button.callback('IP config', 'run_ipconfig'), Markup.button.callback('Tasks', 'run_tasklist')],
    [Markup.button.callback('System info', 'run_systeminfo')],
    [Markup.button.callback('Back', 'menu')]
  ]));
});

for (const name of ['ipconfig', 'tasklist', 'systeminfo']) {
  bot.action(`run_${name}`, async (ctx) => {
    await ctx.answerCbQuery();
    try {
      const { stdout, stderr } = await runAllowedCommand(name);
      const output = `${stdout}${stderr}`.trim().slice(0, 3900);
      await ctx.reply(output || 'No output');
    } catch (error) {
      await ctx.reply(`Command failed: ${error.message}`);
    }
  });
}

for (const action of ['sleep', 'restart', 'shutdown']) {
  bot.action(`confirm_${action}`, async (ctx) => {
    await ctx.answerCbQuery();
    await ctx.reply(
      `Confirm ${action}?`,
      Markup.inlineKeyboard([
        [Markup.button.callback('Yes', `power_${action}`), Markup.button.callback('Cancel', 'menu')]
      ])
    );
  });

  bot.action(`power_${action}`, async (ctx) => {
    await ctx.answerCbQuery();
    await ctx.reply(`${action[0].toUpperCase()}${action.slice(1)} initiated.`);
    await powerAction(action);
  });
}

bot.action('menu', async (ctx) => {
  await ctx.answerCbQuery();
  await ctx.reply('PC Control', mainMenu());
});

bot.catch((error, ctx) => {
  console.error(error);
  ctx?.reply(`Operation failed: ${error.message}`).catch(() => {});
});

async function pollUpdates() {
  let offset = 0;

  while (!pollingAbortController.signal.aborted) {
    try {
      const updates = await bot.telegram.callApi(
        'getUpdates',
        { timeout: 30, offset, allowed_updates: ['message', 'callback_query'] },
        { signal: pollingAbortController.signal }
      );

      if (updates.length > 0) {
        offset = updates[updates.length - 1].update_id + 1;
        await Promise.all(updates.map((update) => bot.handleUpdate(update)));
      }
    } catch (error) {
      if (pollingAbortController.signal.aborted) return;

      const retryable = error?.name === 'FetchError' || error?.code === 429 || error?.code >= 500;
      if (!retryable) throw error;

      const retryAfter = error?.parameters?.retry_after;
      const waitSeconds = Number.isInteger(retryAfter) ? retryAfter + 1 : 5;
      console.error(`Telegram polling failed. Retry in ${waitSeconds}s: ${error.message}`);
      await new Promise((resolve) => setTimeout(resolve, waitSeconds * 1000));
    }
  }
}

async function launchWithRetry() {
  while (!pollingAbortController.signal.aborted) {
    try {
      bot.botInfo = await bot.telegram.getMe();
      await bot.telegram.deleteWebhook({ drop_pending_updates: true });
      console.log(`Bot connected as @${bot.botInfo.username}; polling timeout is 30s`);
      await pollUpdates();
      return;
    } catch (error) {
      if (pollingAbortController.signal.aborted) return;
      const retryAfter = error?.parameters?.retry_after;
      const waitSeconds = Number.isInteger(retryAfter) ? retryAfter + 1 : 35;
      console.error(`Telegram is not ready yet. Retry in ${waitSeconds}s: ${error.message}`);
      await new Promise((resolve) => setTimeout(resolve, waitSeconds * 1000));
    }
  }
}

launchWithRetry();

process.once('SIGINT', () => {
  pollingAbortController.abort();
  bot.stop('SIGINT');
});
process.once('SIGTERM', () => {
  pollingAbortController.abort();
  bot.stop('SIGTERM');
});
