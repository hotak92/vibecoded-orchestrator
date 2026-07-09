// Fastify-style route definitions for CodeAPI coverage in the golden corpus.

function registerRoutes(app) {
  app.route({ secure: true, method: 'POST', url: '/items/create', handler: createItem });
  app.route({ secure: false, method: 'GET', url: '/items/list', handler: listItems });
}

function createItem(req) {
  return { ok: true };
}

function listItems(req) {
  return [];
}

export { registerRoutes };
