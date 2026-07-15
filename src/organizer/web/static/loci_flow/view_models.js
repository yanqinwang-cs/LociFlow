export function buildHomeViewModel(store) {
  return store;
}

export function buildOrganizeViewModel(store) {
  return store;
}

export function buildDuplicateBinViewModel(store) {
  return store;
}

export function buildActivityViewModel(store) {
  return store;
}

export function buildStandardsViewModel(store) {
  return store;
}

export function buildSettingsViewModel(store) {
  return store;
}

export function buildRouteViewModel(store, route) {
  if (route === "organize") return buildOrganizeViewModel(store);
  if (route === "duplicate-bin") return buildDuplicateBinViewModel(store);
  if (route === "activity") return buildActivityViewModel(store);
  if (route === "standards") return buildStandardsViewModel(store);
  if (route === "settings") return buildSettingsViewModel(store);
  return buildHomeViewModel(store);
}

export function buildDialogViewModel(store) {
  return store;
}
