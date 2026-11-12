"""Human references and literal search shared by console model interfaces."""


STANDARD_PROVIDER = "builtin-standard"


def matches_query(query, *values):
    haystack = " ".join(str(value) for value in values if value is not None).casefold()
    return all(word in haystack for word in str(query or "").casefold().split())


def provider_name(data, profile):
    if profile.get("backend", "local") == "local":
        return "Стандартные / GGUF"
    provider = data.get("providers", {}).get(profile.get("provider_id"), {})
    return provider.get("name") or profile.get("base_url") or "API"


def resolve_provider(registry, reference, *, allow_standard=False):
    providers = registry.providers()
    if reference in providers:
        return reference
    folded = str(reference).casefold()
    if allow_standard and folded in {STANDARD_PROVIDER, "standard", "стандартные", "gguf", "local"}:
        return STANDARD_PROVIDER
    candidates = [identifier for identifier, value in providers.items()
                  if str(value.get("name", "")).casefold() == folded]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise ValueError("Такое название есть у нескольких провайдеров. Укажи ID из handy providers list --ids.")
    raise ValueError("Провайдер не найден. Посмотри handy providers list.")


def _belongs(profile, provider):
    if provider is None:
        return True
    if provider == STANDARD_PROVIDER:
        return profile.get("backend", "local") == "local"
    return profile.get("provider_id") == provider


def resolve_model(registry, reference, *, provider=None, kind=None):
    profiles = {identifier: value for identifier, value in registry.profiles().items()
                if _belongs(value, provider) and (kind is None or value["kind"] == kind)}
    if reference in profiles:
        return reference
    # Provider model IDs are case-sensitive. Preserve exact matches before
    # allowing convenient case-insensitive display-name lookup.
    candidates = [identifier for identifier, value in profiles.items() if value.get("model_id") == reference]
    if not candidates:
        folded = str(reference).casefold()
        candidates = [identifier for identifier, value in profiles.items()
                      if str(value.get("name", "")).casefold() == folded]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise ValueError("Найдено несколько моделей. Укажи --provider НАЗВАНИЕ или ID из handy models list --ids.")
    raise ValueError("Модель не найдена. Посмотри handy models search ЗАПРОС или handy models list.")


def filter_profiles(snapshot, *, query="", provider=None, kind=None):
    data = {"providers": {value["id"]: value for value in snapshot.get("providers", [])}}
    return [profile for profile in snapshot.get("profiles", [])
            if _belongs(profile, provider)
            and (kind is None or profile["kind"] == kind)
            and matches_query(query, profile.get("id"), profile.get("name"), profile.get("model_id"),
                              profile.get("model"), provider_name(data, profile))]
