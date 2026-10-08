import { Routes } from '@angular/router';
import { inject } from '@angular/core';
import { adminGuard, authGuard, guestGuard } from './core/guards/auth.guard';
import { AuthService } from './core/services/auth.service';

export const routes: Routes = [
  {
    path: 'login',
    canActivate: [guestGuard],
    loadComponent: () => import('./features/login/login').then((m) => m.Login),
  },
  {
    path: '',
    canActivate: [authGuard],
    loadComponent: () => import('./layout/shell/shell').then((m) => m.Shell),
    children: [
      {
        // El administrador entra al inicio; un corrector, directo a lo suyo.
        path: '',
        pathMatch: 'full',
        redirectTo: () => (inject(AuthService).esAdmin() ? '/inicio' : '/esteno'),
      },
      {
        path: 'inicio',
        canActivate: [adminGuard],
        loadComponent: () => import('./features/inicio/inicio').then((m) => m.Inicio),
      },
      {
        path: 'sesiones',
        canActivate: [adminGuard],
        loadComponent: () => import('./features/sesiones/lista/sesiones-lista').then((m) => m.SesionesLista),
      },
      {
        path: 'sesiones/:id',
        canActivate: [adminGuard],
        loadComponent: () => import('./features/sesiones/revision/revision').then((m) => m.Revision),
      },
      {
        path: 'transcribir',
        canActivate: [adminGuard],
        loadComponent: () => import('./features/transcribir/transcribir').then((m) => m.Transcribir),
      },
      {
        path: 'esteno',
        loadComponent: () => import('./features/esteno/esteno-selector').then((m) => m.EstenoSelector),
      },
      {
        path: 'esteno/:id',
        loadComponent: () => import('./features/esteno/esteno-sesion').then((m) => m.EstenoSesion),
      },
      {
        path: 'usuarios',
        canActivate: [adminGuard],
        loadComponent: () => import('./features/usuarios/usuarios').then((m) => m.Usuarios),
      },
    ],
  },
  { path: '**', redirectTo: '' },
];
